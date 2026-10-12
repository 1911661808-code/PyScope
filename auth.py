"""服务器认证：不可预测会话、服务器端撤销、CSRF 与有界登录限流。"""
from contextlib import contextmanager
import hashlib
import hmac
import secrets
import re
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from flask import g, jsonify, redirect, render_template, request
from werkzeug.security import check_password_hash, generate_password_hash

COOKIE = 'pyscope_session'


def validate_origins(origins, secure):
    if not isinstance(origins, list) or not origins or len(origins) > 8:
        raise ValueError('allowed_origins 需要 1–8 个明确的访问地址。')
    for origin in origins:
        if not isinstance(origin, str):
            raise ValueError('访问地址必须为字符串。')
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path or '*' in origin or origin != f'{parsed.scheme}://{parsed.netloc}'
                or any(c.isspace() for c in origin)):
            raise ValueError('访问地址必须精确到 scheme://host[:port]，不带路径或通配符。')
        _ = parsed.port
        if parsed.scheme == 'http' and (parsed.hostname not in ('localhost', '127.0.0.1', '::1') or secure):
            raise ValueError('HTTP 仅允许 SSH 隧道的回环地址，且 secure_cookie 必须为 false。')
        if parsed.scheme == 'https' and not secure:
            raise ValueError('HTTPS 访问必须启用 secure_cookie。')
    return {urlsplit(value).netloc.lower() for value in origins}


class Auth:
    def __init__(self, app, directory, config):
        self.config = config
        self.db = directory / 'auth.sqlite3'
        self.secret = config['secret_key'].encode()
        self.users = config['users']
        self.hours = config.get('session_hours', 8)
        self.hosts = validate_origins(config['allowed_origins'], config['secure_cookie'])
        self.password_gate = threading.BoundedSemaphore(2)
        self.dummy_hash = generate_password_hash(secrets.token_urlsafe(32))
        with self.connection() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT, csrf TEXT, expires REAL, credential TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS attempts (key TEXT PRIMARY KEY, count INTEGER, expires REAL)')
        app.before_request(self.guard)
        app.after_request(self.headers)
        app.add_url_rule('/login', 'login', self.login_page)
        app.add_url_rule('/auth/login', 'auth_login', self.login, methods=['POST'])
        app.add_url_rule('/auth/logout', 'auth_logout', self.logout, methods=['POST'])

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def digest(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def issue(self, username=None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        duration = self.hours * 3600 if username else 600
        credential = self.digest(self.users[username]) if username else ''
        with self.connection() as conn:
            conn.execute('DELETE FROM sessions WHERE expires<?', (now,))
            conn.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (self.digest(token), username, csrf, now + duration, credential))
            # 匿名登录页面和已登录会话分别有界，避免匿名请求挤掉活跃用户。
            conn.execute('DELETE FROM sessions WHERE username IS NULL AND token NOT IN (SELECT token FROM sessions WHERE username IS NULL ORDER BY rowid DESC LIMIT 2000)')
            conn.execute('DELETE FROM sessions WHERE username IS NOT NULL AND token NOT IN (SELECT token FROM sessions WHERE username IS NOT NULL ORDER BY rowid DESC LIMIT 2000)')
        g.auth = {'username': username, 'csrf': csrf, 'token': self.digest(token)}
        g.new_cookie = (token, duration)
        return csrf

    def revoke(self):
        if g.get('auth'):
            with self.connection() as conn:
                conn.execute('DELETE FROM sessions WHERE token=?', (g.auth['token'],))
        g.auth = None
        g.new_cookie = ('', 0)

    @staticmethod
    def error(message, status):
        return jsonify(error=message, message=message), status

    def guard(self):
        g.auth = None
        if request.host.lower() not in self.hosts:
            return self.error('访问地址不在服务器允许列表中。', 403)
        origin = request.headers.get('Origin')
        if origin and origin not in self.config['allowed_origins']:
            return self.error('不允许跨站请求。', 403)
        token = request.cookies.get(COOKIE, '')
        if token and len(token) < 128:
            with self.connection() as conn:
                row = conn.execute('SELECT username,csrf,expires,credential FROM sessions WHERE token=?', (self.digest(token),)).fetchone()
            if row and row[2] > time.time() and (row[0] is None or (
                    row[0] in self.users and hmac.compare_digest(row[3], self.digest(self.users[row[0]])))):
                g.auth = {'username': row[0], 'csrf': row[1], 'token': self.digest(token)}
        public = request.path in ('/login', '/auth/login') or request.path.startswith('/static/')
        if not public and not (g.auth and g.auth['username']):
            if request.path == '/':
                return redirect('/login')
            return self.error('请登录后继续。', 401)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not request.is_json:
                return self.error('请求必须使用 application/json。', 415)
            csrf = request.headers.get('X-CSRF-Token', '')
            if not g.auth or len(csrf) > 128 or not hmac.compare_digest(csrf.encode(), g.auth['csrf'].encode()):
                return self.error('页面验证已失效，请刷新页面后重试。', 403)
        g.username = g.auth['username'] if g.auth else None

    def headers(self, response):
        if g.get('new_cookie') is not None:
            token, age = g.new_cookie
            response.set_cookie(COOKIE, token, max_age=age, secure=self.config['secure_cookie'],
                                httponly=True, samesite='Strict', path='/')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if self.config['secure_cookie']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def login_page(self):
        if g.auth and g.auth['username']:
            return redirect('/')
        csrf = g.auth['csrf'] if g.auth else self.issue()
        return render_template('login.html', csrf_token=csrf)

    def allow_attempt(self, username):
        now = time.time()
        keys = [('global', 60), ('ip:' + (request.remote_addr or ''), 15), ('user:' + username, 8)]
        with self.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM attempts WHERE expires<?', (now,))
            for key, limit in keys:
                row = conn.execute('SELECT count FROM attempts WHERE key=?', (self.digest(key),)).fetchone()
                if row and row[0] >= limit:
                    return False
            if conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] > 5000:
                return False
            for key, _ in keys:
                conn.execute('INSERT INTO attempts VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET count=count+1',
                             (self.digest(key), 1, now + 300))
        return True

    def login(self):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return self.error('请求内容应为 JSON 对象。', 400)
        username, password = payload.get('username'), payload.get('password')
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 1024:
            return self.error('账号或密码不正确。', 401)
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', username):
            return self.error('账号或密码不正确。', 401)
        try:
            password.encode('utf-8')
        except UnicodeEncodeError:
            return self.error('账号或密码不正确。', 401)
        if not self.allow_attempt(username) or not self.password_gate.acquire(blocking=False):
            response, status = self.error('登录尝试过于频繁，请 5 分钟后重试。', 429)
            response.headers['Retry-After'] = '300'
            return response, status
        try:
            valid = check_password_hash(self.users.get(username, self.dummy_hash), password)
        finally:
            self.password_gate.release()
        if not valid or username not in self.users:
            return self.error('账号或密码不正确。', 401)
        self.revoke()
        self.issue(username)
        return jsonify(ok=True, next='/')

    def logout(self):
        self.revoke()
        return jsonify(ok=True, next='/login')
