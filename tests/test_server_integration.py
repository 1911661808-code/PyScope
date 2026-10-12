"""真实 Linux 服务器入口联调；必须显式启用并使用专用 rootless Docker。"""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from werkzeug.security import generate_password_hash


ROOT = Path(__file__).resolve().parents[1]
ENABLED = (
    sys.platform == 'linux'
    and os.environ.get('PYSCOPE_DOCKER_TESTS') == '1'
    and os.environ.get('PYSCOPE_REQUIRE_ROOTLESS') == '1'
)


@unittest.skipUnless(ENABLED, '需要 Linux 和显式启用的真实 rootless Docker 服务器集成测试')
class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.process = None
        self.log = None
        self.directory = None
        self.exclusive_daemon = False
        self.sensitive = []
        self.addCleanup(self.cleanup_resources)
        self.assertNotEqual(os.geteuid(), 0, '真实服务器测试必须由非 root 账号运行')
        # server.py 会在专用 daemon 中恢复重启遗留状态，因此不能与其他实例并行。
        self.assertFalse(self.container_ids(), '请使用没有活跃 PyScope 实例的专用 rootless daemon')
        self.exclusive_daemon = True
        self.directory = tempfile.TemporaryDirectory(prefix='pyscope-server-test-')
        base = Path(self.directory.name)
        base.chmod(0o700)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.origin = f'http://localhost:{port}'
        self.password = secrets.token_urlsafe(24)
        password_hash = generate_password_hash(self.password, method='scrypt:32768:8:1')
        secret = secrets.token_urlsafe(32)
        self.sensitive.extend((self.password, password_hash, secret))
        config = dict(
            version=1, secret_key=secret, users={'server_test': password_hash},
            allowed_origins=[self.origin], secure_cookie=False,
            data_dir=str(base / 'data'), port=port, require_rootless=True, session_hours=1,
            docker_image=os.environ.get('PYSCOPE_WORKER_IMAGE', 'pyscope-worker:1.1.0'),
            docker_runtime=os.environ.get('PYSCOPE_DOCKER_RUNTIME') or None,
        )
        config_path = base / 'server.json'
        descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(config, stream)
        self.log_path = base / 'server.log'
        self.log = self.log_path.open('wb')
        environment = dict(os.environ, PYTHONUNBUFFERED='1')
        self.process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', str(ROOT / 'server.py'),
             '--config', str(config_path), 'serve'],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            env=environment,
        )
        self.cookies = http.cookiejar.CookieJar()
        self.client = self.new_client(self.cookies)
        self.anonymous = self.new_client(http.cookiejar.CookieJar())
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail('server.py 启动退出：\n' + self.server_log())
            try:
                status, page = self.request('/login', decode_json=False)
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(.1)
                continue
            self.assertEqual(status, 200, '服务器登录页不可用：\n' + self.server_log())
            self.login_page = page
            break
        else:
            self.fail('等待 server.py 启动超时：\n' + self.server_log())

    @staticmethod
    def new_client(jar):
        # 本机回环请求不经过 CI 环境可能配置的外部 HTTP 代理。
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))

    def server_log(self):
        if not self.log_path.exists():
            return '(没有服务器日志)'
        with self.log_path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 16_384))
            text = stream.read().decode('utf-8', errors='replace')
        for value in self.sensitive:
            text = text.replace(value, '<redacted>')
        return text

    def container_ids(self):
        result = subprocess.run(
            ['docker', 'ps', '--all', '--quiet', '--no-trunc', '--filter', 'label=pyscope.worker=true'],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, '无法枚举测试 daemon 的 PyScope 容器')
        identifiers = set(result.stdout.split())
        self.assertTrue(all(re.fullmatch(r'[0-9a-f]{64}', value) for value in identifiers),
                        'Docker 返回无效容器标识')
        return identifiers

    def cleanup_resources(self):
        # 即使断言失败，也先停止服务器，再清理本测试专用 daemon 的残留。
        try:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.exclusive_daemon:
                for identifier in self.container_ids():
                    result = subprocess.run(['docker', 'rm', '--force', identifier],
                                            capture_output=True, text=True, timeout=15, check=False)
                    self.assertTrue(result.returncode == 0 or 'no such container' in result.stderr.lower(),
                                    '测试清理临时容器失败')
        finally:
            if self.log is not None:
                self.log.close()
            if self.directory is not None:
                self.directory.cleanup()

    def request(self, path, *, method='GET', payload=None, csrf=None, client=None, decode_json=True):
        headers = {'Origin': self.origin}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if csrf is not None:
            headers['X-CSRF-Token'] = csrf
        request = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            response = (client or self.client).open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read(1_000_001)
            self.assertLessEqual(len(content), 1_000_000, '服务器测试响应超过预期大小')
            decoded = content.decode('utf-8')
            return response.status, json.loads(decoded) if decode_json else decoded

    def wait_state(self, sid, expected, *, line=None):
        deadline = time.monotonic() + 20
        state = None
        while time.monotonic() < deadline:
            status, state = self.request('/api/sessions/' + sid)
            self.assertEqual(status, 200, str(state))
            if state['status'] in expected:
                if line is None or (state['events'] and state['events'][-1]['line'] == line):
                    return state
            if state['status'] in {'completed', 'error', 'stopped', 'limited'}:
                self.fail('调试意外提前结束：' + str(state.get('error') or state['status']))
            time.sleep(.05)
        self.fail('等待调试状态超时：' + str(state and state['status']) + '\n' + self.server_log())

    def test_server_cli_login_execution_history_logout_and_sigterm_cleanup(self):
        status, _ = self.request('/api/auth/me', client=self.anonymous)
        self.assertEqual(status, 401)
        status, _ = self.request('/api/profile', method='POST', payload={'code': 'print(1)'},
                                 client=self.anonymous)
        self.assertEqual(status, 401)

        match = re.search(r'name="csrf-token" content="([^"]+)"', self.login_page)
        self.assertIsNotNone(match, '登录页面缺少 CSRF token')
        login_csrf = match.group(1)
        self.sensitive.append(login_csrf)
        anonymous_cookie = next(cookie.value for cookie in self.cookies if cookie.name == 'pyscope_session')
        self.sensitive.append(anonymous_cookie)
        status, result = self.request('/auth/login', method='POST', csrf=login_csrf,
                                      payload={'username': 'server_test', 'password': self.password})
        self.assertEqual(status, 200, str(result))
        self.assertTrue(result['ok'])
        status, result = self.request('/api/auth/me')
        self.assertEqual(status, 200)
        self.assertEqual(result['username'], 'server_test')
        self.assertTrue(result['server_mode'])
        csrf = result['csrf_token']
        self.sensitive.append(csrf)
        self.assertTrue(csrf != login_csrf, '登录后必须轮换 CSRF token')
        cookie = next(cookie for cookie in self.cookies if cookie.name == 'pyscope_session')
        self.sensitive.append(cookie.value)
        self.assertTrue(cookie.value != anonymous_cookie, '登录后必须轮换会话 cookie')
        self.assertTrue(cookie.has_nonstandard_attr('HttpOnly'))
        self.assertEqual(cookie.get_nonstandard_attr('SameSite'), 'Strict')
        status, result = self.request('/api/health')
        self.assertEqual((status, result['mode']), (200, 'server'))

        status, _ = self.request('/api/analyze', method='POST', payload={'code': 'x=1'})
        self.assertEqual(status, 403, '服务器写入接口必须验证 CSRF')
        status, result = self.request('/api/analyze', method='POST', csrf=csrf,
                                      payload={'code': 'def f():\n    return 2\nf()\n'})
        self.assertEqual(status, 200, str(result))
        self.assertEqual(result['counts']['functions'], 1)
        status, result = self.request('/api/profile', method='POST', csrf=csrf,
                                      payload={'code': 'def f():\n    return 2\nprint(f())\n'})
        self.assertEqual(status, 200, str(result))
        self.assertEqual(result['output'], '2\n')
        self.assertTrue(result['functions'])

        status, result = self.request('/api/sessions', method='POST', csrf=csrf,
                                      payload={'code': 'a=2\nb=3\nprint(a+b)\n', 'breakpoints': [3]})
        self.assertEqual(status, 201, str(result))
        sid = result['id']
        self.wait_state(sid, {'paused'}, line=1)
        status, result = self.request(f'/api/sessions/{sid}/command', method='POST', csrf=csrf,
                                      payload={'command': 'run'})
        self.assertEqual(status, 200, str(result))
        self.wait_state(sid, {'paused'}, line=3)
        status, result = self.request(f'/api/sessions/{sid}/command', method='POST', csrf=csrf,
                                      payload={'command': 'run'})
        self.assertEqual(status, 200, str(result))
        state = self.wait_state(sid, {'completed'})
        self.assertEqual(state['events'][-1]['output'], '5\n')
        status, result = self.request('/api/history')
        self.assertEqual(status, 200)
        self.assertIn(sid, [item['id'] for item in result['history']])
        status, result = self.request('/api/history/' + sid)
        self.assertEqual((status, result['status']), (200, 'completed'))
        status, result = self.request(f'/api/sessions/{sid}/export')
        self.assertEqual((status, result['format']), (200, 'pyscope-trace-v1'))

        # 故意留下等待输入的真实 worker，证明进程退出处理会清理暂停容器。
        status, result = self.request('/api/sessions', method='POST', csrf=csrf,
                                      payload={'code': 'print("shutdown cleanup")\n'})
        self.assertEqual(status, 201, str(result))
        self.wait_state(result['id'], {'paused'}, line=1)
        self.assertTrue(self.container_ids(), '暂停会话应保留一个实际运行的 worker 容器')
        status, result = self.request('/auth/logout', method='POST', csrf=csrf, payload={})
        self.assertEqual(status, 200, str(result))
        self.assertTrue(result['ok'])
        status, _ = self.request('/api/auth/me')
        self.assertEqual(status, 401)

        self.process.send_signal(signal.SIGTERM)
        try:
            returncode = self.process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            self.fail('SIGTERM 后服务器未按时退出：\n' + self.server_log())
        self.assertEqual(returncode, 0, 'SIGTERM 退出异常：\n' + self.server_log())
        self.assertFalse(self.container_ids(), 'SIGTERM 后仍残留 PyScope worker 容器')


if __name__ == '__main__':
    unittest.main(verbosity=2)
