"""认证集成测试使用显式替代后端；真实容器另见 test_docker_integration。"""
import copy
import re
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from auth import COOKIE
from execution_backend import DockerBackend, LocalBackend
from server import validate_config, save_new, load_config
from session_manager import SessionManager
from werkzeug.security import generate_password_hash


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = 'test-password-only-12345'
        cls.hashed = generate_password_hash(cls.password, method='scrypt:32768:8:1')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dict(version=1, secret_key=secrets.token_urlsafe(32), users={'alice': self.hashed, 'bob': self.hashed},
                           allowed_origins=['http://localhost'], secure_cookie=False,
                           data_dir=self.temp.name, port=9000, require_rootless=True, session_hours=8)
        # 明确替代容器启动方法，只验证认证/授权与API；绝不启用真实服务器入口。
        self.backend = DockerBackend.__new__(DockerBackend)
        local = LocalBackend()
        self.patches = [patch.object(self.backend, name, getattr(local, name)) for name in ('spawn_trace', 'analyze', 'run_profile', 'close')]
        self.patches.append(patch.object(self.backend, 'verify', return_value={'ok': True}))
        for patched in self.patches:
            patched.start()
        self.manager = SessionManager(backend=self.backend)
        self.app = create_app(self.temp.name, self.manager, self.config)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['shutdown']()
        for patched in self.patches:
            patched.stop()
        self.temp.cleanup()

    def login(self, client=None, username='alice'):
        client = client or self.client
        page = client.get('/login')
        csrf = re.search(rb'name="csrf-token" content="([^"]+)"', page.data)[1].decode()
        old = client.get_cookie(COOKIE).value
        response = client.post('/auth/login', json={'username': username, 'password': self.password}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        me = client.get('/api/auth/me').get_json()
        self.assertEqual(me['username'], username)
        return me['csrf_token'], old

    def test_authentication_required_every_execution_and_history_route(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        for path in ('/api/auth/me', '/api/health', '/api/examples', '/api/history', '/api/history/id', '/api/sessions/id', '/api/sessions/id/export'):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        for path in ('/api/analyze', '/api/profile', '/api/sessions', '/api/sessions/id/command'):
            self.assertEqual(self.client.post(path, json={'code': 'x=1'}).status_code, 401, path)

    def test_rotation_csrf_logout_revokes_cookie(self):
        csrf, anonymous = self.login()
        authenticated = self.client.get_cookie(COOKIE).value
        self.assertNotEqual(authenticated, anonymous)
        for method, path, body in [('post', '/api/analyze', {'code': 'x=1'}), ('delete', '/api/history/id', None), ('post', '/auth/logout', {})]:
            self.assertEqual(getattr(self.client, method)(path, json=body).status_code in (403, 415), True)
        self.assertEqual(self.client.post('/api/analyze', json={'code': 'x=1'}, headers={'X-CSRF-Token': csrf}).status_code, 200)
        self.assertEqual(self.client.post('/auth/logout', json={}, headers={'X-CSRF-Token': csrf}).status_code, 200)
        self.client.set_cookie(COOKIE, authenticated)
        self.assertEqual(self.client.get('/api/history').status_code, 401)

    def test_cross_origin_bad_host_and_cookie_flags(self):
        page = self.client.get('/login')
        self.assertIn('HttpOnly', page.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', page.headers['Set-Cookie'])
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", page.headers['Content-Security-Policy'])
        self.assertEqual(self.client.get('/login', headers={'Host': 'evil.example'}).status_code, 403)
        csrf, _ = self.login()
        self.assertEqual(self.client.post('/api/analyze', json={'code': 'x=1'}, headers={'X-CSRF-Token': csrf, 'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/analyze', json={'code': 'x=1'}, headers={'X-CSRF-Token': '错误'}).status_code, 403)

    def test_wrong_password_rate_limit_and_expiry(self):
        page = self.client.get('/login')
        csrf = re.search(rb'name="csrf-token" content="([^"]+)"', page.data)[1].decode()
        for _ in range(8):
            response = self.client.post('/auth/login', json={'username': 'alice', 'password': 'incorrect'}, headers={'X-CSRF-Token': csrf})
            self.assertEqual(response.status_code, 401)
        response = self.client.post('/auth/login', json={'username': 'alice', 'password': self.password}, headers={'X-CSRF-Token': csrf})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '300')
        with self.app.extensions['auth'].connection() as conn:
            conn.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.client.post('/auth/login', json={}, headers={'X-CSRF-Token': csrf}).status_code, 403)

    def test_credentials_change_revokes_existing_session(self):
        self.login()
        self.app.extensions['auth'].users['alice'] = 'changed'
        self.assertEqual(self.client.get('/api/history').status_code, 401)

    def test_invalid_unicode_and_session_expiry(self):
        csrf, _ = self.login()
        for path in ('/api/analyze', '/api/profile', '/api/sessions'):
            self.assertEqual(self.client.post(path, data=b'{"code":"\\ud800"}', content_type='application/json', headers={'X-CSRF-Token': csrf}).status_code, 400)
        with self.app.extensions['auth'].connection() as conn:
            conn.execute('UPDATE sessions SET expires=0')
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)

    def test_secure_cookie_and_origin_policy(self):
        auth = self.app.extensions['auth']
        auth.config['secure_cookie'] = True
        page = self.client.get('/login')
        self.assertIn('Secure', page.headers['Set-Cookie'])
        self.assertEqual(page.headers['Strict-Transport-Security'], 'max-age=31536000')
        for origin in ('https://example.com/path', 'https://*.example.com', 'http://example.com', 'https://user@example.com'):
            config = copy.deepcopy(self.config)
            config['allowed_origins'] = [origin]
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_ownership_and_history_isolation(self):
        alice_csrf, _ = self.login()
        bob = self.app.test_client()
        bob_csrf, _ = self.login(bob, 'bob')
        response = self.client.post('/api/sessions', json={'code': 'x=1\nprint(x)'}, headers={'X-CSRF-Token': alice_csrf})
        self.assertEqual(response.status_code, 201)
        sid = response.get_json()['id']
        for path in (f'/api/sessions/{sid}', f'/api/sessions/{sid}/export'):
            self.assertEqual(bob.get(path).status_code, 404)
        self.assertEqual(bob.post(f'/api/sessions/{sid}/command', json={'command': 'stop'}, headers={'X-CSRF-Token': bob_csrf}).status_code, 404)
        self.client.post(f'/api/sessions/{sid}/command', json={'command': 'stop'}, headers={'X-CSRF-Token': alice_csrf})
        for _ in range(100):
            if self.client.get('/api/history').get_json()['history']:
                break
            time.sleep(.02)
        self.assertEqual(self.client.get('/api/history').get_json()['history'][0]['id'], sid)
        self.assertEqual(bob.get('/api/history').get_json()['history'], [])
        self.assertEqual(bob.get(f'/api/history/{sid}').status_code, 404)
        self.assertEqual(bob.delete(f'/api/history/{sid}', headers={'X-CSRF-Token': bob_csrf}, json={}).status_code, 404)
        self.assertEqual(self.client.get(f'/api/history/{sid}').status_code, 200)

    def test_per_user_active_limit(self):
        csrf, _ = self.login()
        for _ in range(2):
            self.assertEqual(self.client.post('/api/sessions', json={'code': 'x=1'}, headers={'X-CSRF-Token': csrf}).status_code, 201)
        self.assertEqual(self.client.post('/api/sessions', json={'code': 'x=1'}, headers={'X-CSRF-Token': csrf}).status_code, 429)

    def test_reject_local_backend_and_unsafe_config(self):
        local_manager = SessionManager()
        try:
            with self.assertRaises(ValueError):
                create_app(self.temp.name, local_manager, self.config)
        finally:
            local_manager.close()
        for changes in ({'allowed_origins': ['http://public.example']}, {'allowed_origins': ['https://public.example']},
                        {'secret_key': ''}, {'users': {'admin': 'plaintext'}}, {'require_rootless': False}):
            config = copy.deepcopy(self.config)
            config.update(changes)
            with self.assertRaises(ValueError):
                validate_config(config)
        path = Path(self.temp.name) / 'config.json'
        save_new(path, self.config)
        self.assertEqual(load_config(path), self.config)
        with self.assertRaises(FileExistsError):
            save_new(path, self.config)


if __name__ == '__main__':
    unittest.main()
