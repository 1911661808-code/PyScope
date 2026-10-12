"""PyScope 本地 Web 服务：真实调试、结构分析、性能分析和本地历史。"""
import argparse
import atexit
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import webbrowser

from flask import Flask, g, jsonify, render_template, request, send_file
from analysis_engine import analyze, validate_code
from examples import EXAMPLES
from session_manager import SessionManager

ROOT = Path(__file__).resolve().parent
TERMINAL = {'completed', 'error', 'stopped', 'limited'}


def create_app(data_dir=None, manager=None, server_config=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 150_000
    app.json.ensure_ascii = False
    sessions = manager or SessionManager()
    server_mode = server_config is not None
    if server_mode:
        from execution_backend import DockerBackend
        from server import validate_config
        validate_config(server_config)
        if not isinstance(sessions.backend, DockerBackend):
            raise ValueError('服务器模式必须使用 DockerBackend，禁止回退到本地进程。')
        sessions.backend.verify(require_rootless=True)
    app.extensions['sessions'] = sessions
    directory = Path(data_dir or os.environ.get('PYSCOPE_DATA_DIR', ROOT / 'data'))
    directory.mkdir(parents=True, exist_ok=True)
    if server_mode:
        from auth import Auth
        app.extensions['auth'] = Auth(app, directory, server_config)
    db = directory / 'history.sqlite3'
    lock = threading.Lock()
    persisted = set()
    pending = set()
    owners = {}
    admission_lock = threading.Lock()
    execution_slots = threading.BoundedSemaphore(4)
    shutdown_event = threading.Event()

    @contextmanager
    def connection():
        conn = sqlite3.connect(db)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with connection() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS history (id TEXT PRIMARY KEY, title TEXT, status TEXT, event_count INTEGER, created_at TEXT, payload TEXT)')
        if 'owner' not in {row[1] for row in conn.execute('PRAGMA table_info(history)')}:
            conn.execute('ALTER TABLE history ADD COLUMN owner TEXT')
        conn.execute('CREATE INDEX IF NOT EXISTS history_owner ON history(owner)')

    def owner():
        return g.username if server_mode else None

    def owned(sid):
        if server_mode and owners.get(sid) != owner():
            raise KeyError(sid)

    @contextmanager
    def execution_slot():
        if server_mode and not execution_slots.acquire(blocking=False):
            from werkzeug.exceptions import TooManyRequests
            raise TooManyRequests('分析任务繁忙，请稍后重试。')
        try:
            yield
        finally:
            if server_mode:
                execution_slots.release()

    def persist(state):
        if state['status'] not in TERMINAL:
            return
        with lock:
            if state['id'] in persisted:
                pending.discard(state['id'])
                return
            # 增量结果不含完整事件，终态时只写入一次完整快照。
            complete = sessions.get(state['id'], -1)
            lines = [line.strip() for line in complete.get('code', '').splitlines() if line.strip()]
            title = (lines[0] if lines else '未命名程序')[:65]
            serialized = json.dumps(complete, ensure_ascii=False)
            with connection() as conn:
                conn.execute('INSERT OR IGNORE INTO history (id,title,status,event_count,created_at,payload,owner) VALUES (?,?,?,?,?,?,?)',
                             (state['id'], title, complete['status'], complete['total_events'],
                              str(complete.get('created_at', '')), serialized, owners.get(state['id'])))
                conn.execute('DELETE FROM history WHERE owner IS ? AND id NOT IN (SELECT id FROM history WHERE owner IS ? ORDER BY rowid DESC LIMIT 100)',
                             (owners.get(state['id']), owners.get(state['id'])))
                conn.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY rowid DESC LIMIT 1000)')
                if server_mode:
                    # 同时按总数据量淘汰最旧记录，避免大快照耗尽服务器磁盘。
                    rows = conn.execute('SELECT id,LENGTH(CAST(payload AS BLOB)) FROM history ORDER BY rowid DESC').fetchall()
                    total = 0
                    for row_id, size in rows:
                        total += size
                        if total > 128 * 1024 * 1024:
                            conn.execute('DELETE FROM history WHERE id=?', (row_id,))
            persisted.add(state['id'])
            pending.discard(state['id'])

    def collect_terminal():
        with lock:
            ids = tuple(pending)
        for sid in ids:
            try:
                persist(sessions.get(sid, 10**12))
            except KeyError:
                with lock:
                    pending.discard(sid)

    def collect_loop():
        while not shutdown_event.wait(.2):
            try:
                collect_terminal()
            except Exception:
                app.logger.exception('保存调试记录失败')

    collector = threading.Thread(target=collect_loop, name='pyscope-history', daemon=True)
    collector.start()

    def shutdown():
        shutdown_event.set()
        sessions.close()
        collector.join(timeout=2)
        collect_terminal()

    app.extensions['shutdown'] = shutdown

    @app.before_request
    def local_only():
        if server_mode:
            return None
        # 防止浏览器其他站点跨源调用本地执行接口；这不是代码安全沙箱。
        if request.host.split(':')[0] not in ('localhost', '127.0.0.1'):
            return jsonify(error='只允许通过 localhost 或 127.0.0.1 访问。'), 403
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            return jsonify(error='不允许跨源访问本地执行接口。'), 403
        if request.path.startswith('/api/') and request.method == 'POST' and not request.is_json:
            return jsonify(error='请求必须使用 application/json。'), 415

    @app.after_request
    def response_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    from execution_backend import ExecutionTimeout

    @app.errorhandler(ExecutionTimeout)
    def execution_timeout(exc):
        return jsonify(error=str(exc)), 408

    @app.errorhandler(429)
    def busy(exc):
        return jsonify(error=exc.description, message=exc.description), 429

    @app.errorhandler(KeyError)
    def missing(exc):
        return jsonify(error='找不到该运行记录，请重新启动调试。'), 404

    @app.errorhandler(413)
    def too_large(exc):
        return jsonify(error='提交内容过大，请将代码控制在 30000 字符以内。'), 413

    @app.errorhandler(SyntaxError)
    def syntax_error(exc):
        return jsonify(error=f'语法错误：{exc.msg}', line=exc.lineno, offset=exc.offset), 400

    def body():
        result = request.get_json(silent=True)
        if not isinstance(result, dict):
            raise ValueError('请求内容应为 JSON 对象。')
        return result

    def checked_breakpoints(points):
        if not isinstance(points, list) or any(type(n) is not int or n < 1 or n > 30_000 for n in points):
            raise ValueError('断点必须是正整数行号列表。')
        return sorted(set(points))

    @app.get('/')
    def index():
        return render_template('index.html', server_mode=server_mode,
                               current_username=owner(), csrf_token=g.auth['csrf'] if server_mode else '')

    @app.get('/api/auth/me')
    def me():
        return jsonify(authenticated=True, username=owner(), server_mode=server_mode,
                       csrf_token=g.auth['csrf'] if server_mode else None)

    @app.get('/api/health')
    def health():
        return jsonify(ok=True, python=sys.version.split()[0], mode='server' if server_mode else 'local')

    @app.get('/api/examples')
    def examples():
        return jsonify(examples=EXAMPLES)

    @app.post('/api/analyze')
    def structure():
        code = validate_code(body().get('code'))
        with execution_slot():
            return jsonify(sessions.backend.analyze(code))

    @app.post('/api/sessions')
    def start():
        data = body()
        code = validate_code(data.get('code'))
        points = checked_breakpoints(data.get('breakpoints', []))
        with execution_slot(), admission_lock:
            if server_mode:
                active = 0
                for known, username in tuple(owners.items()):
                    try:
                        prior = sessions.get(known, 10**12)
                        if username == owner() and prior['status'] not in TERMINAL:
                            active += 1
                    except KeyError:
                        if known not in pending:
                            owners.pop(known, None)
                            persisted.discard(known)
                if active >= 2:
                    from werkzeug.exceptions import TooManyRequests
                    raise TooManyRequests('每个账号最多同时保留两个调试会话，请先停止已有会话。')
                sessions.backend.analyze(code)
            else:
                compile(code, '<pyscope>', 'exec')
            sid = sessions.start(code, points)
            with lock:
                owners[sid] = owner()
                pending.add(sid)
        return jsonify(sessions.get(sid, -1)), 201

    @app.get('/api/sessions/<sid>')
    def state(sid):
        owned(sid)
        try:
            after = int(request.args.get('after', '-1'))
        except (ValueError, TypeError):
            raise ValueError('after 必须为整数。')
        result = sessions.get(sid, after)
        persist(result)
        return jsonify(result)

    @app.post('/api/sessions/<sid>/command')
    def command(sid):
        data = body()
        if not isinstance(data.get('command'), str):
            raise ValueError('command 必须为 step、run、pause 或 stop 字符串。')
        owned(sid)
        points = checked_breakpoints(data['breakpoints']) if 'breakpoints' in data else None
        result = sessions.command(sid, data.get('command'), points)
        persist(result)
        return jsonify(result)

    @app.get('/api/sessions/<sid>/export')
    def export(sid):
        try:
            owned(sid)
            result = sessions.get(sid, -1)
        except KeyError:
            with connection() as conn:
                record = conn.execute('SELECT payload FROM history WHERE id=? AND owner IS ?', (sid, owner())).fetchone()
            if not record:
                raise KeyError(sid)
            result = json.loads(record[0])
        payload = json.dumps({'format': 'pyscope-trace-v1', **result}, ensure_ascii=False, indent=2).encode('utf-8')
        return send_file(io.BytesIO(payload), mimetype='application/json', as_attachment=True,
                         download_name=f'pyscope-{sid[:8]}.json')

    @app.post('/api/profile')
    def profile():
        code = validate_code(body().get('code'))
        with execution_slot():
            if not server_mode:
                compile(code, '<pyscope>', 'exec')
            return jsonify(sessions.backend.run_profile(code))

    @app.get('/api/history')
    def history():
        with connection() as conn:
            rows = conn.execute('SELECT id,title,status,event_count,created_at FROM history WHERE owner IS ? ORDER BY rowid DESC LIMIT 100', (owner(),)).fetchall()
        return jsonify(history=[dict(zip(('id','title','status','event_count','created_at'), row)) for row in rows])

    @app.get('/api/history/<sid>')
    def history_item(sid):
        with connection() as conn:
            row = conn.execute('SELECT payload FROM history WHERE id=? AND owner IS ?', (sid, owner())).fetchone()
        if not row:
            raise KeyError(sid)
        return jsonify(json.loads(row[0]))

    @app.delete('/api/history/<sid>')
    def history_delete(sid):
        with lock, connection() as conn:
            if server_mode and not conn.execute('SELECT 1 FROM history WHERE id=? AND owner=?', (sid, owner())).fetchone():
                raise KeyError(sid)
            conn.execute('DELETE FROM history WHERE id=? AND owner IS ?', (sid, owner()))
            persisted.add(sid)
            pending.discard(sid)
        return jsonify(ok=True)

    return app


if __name__ == '__main__':
    if '--trace-worker' in sys.argv:
        from trace_worker import main as worker_main
        worker_main()
        raise SystemExit(0)
    if '--profile-worker' in sys.argv:
        from profile_worker import main as worker_main
        worker_main()
        raise SystemExit(0)
    parser = argparse.ArgumentParser(description='PyScope 本地程序执行可视化平台')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true', help='启动后打开默认浏览器')
    parser.add_argument('--no-open', action='store_true', help='不自动打开浏览器，供命令行或自动验证使用')
    args = parser.parse_args()
    if getattr(sys, 'frozen', False) and 'PYSCOPE_DATA_DIR' not in os.environ:
        os.environ['PYSCOPE_DATA_DIR'] = str(Path(sys.executable).parent / 'data')
    application = create_app()
    atexit.register(application.extensions['shutdown'])
    print(f'PyScope: http://127.0.0.1:{args.port}', flush=True)
    if not args.no_open and (args.open or getattr(sys, 'frozen', False)):
        threading.Timer(1.2, lambda: webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    application.run(host='127.0.0.1', port=args.port, debug=False, threaded=True, use_reloader=False)
