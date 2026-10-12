"""通过 Flask 测试客户端验证真实子进程链路，无模拟调试结果。"""
import json
import tempfile
import time
import unittest

from app import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(data_dir=self.temp.name)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions['shutdown']()
        self.temp.cleanup()

    def wait(self, sid, statuses, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = self.client.get('/api/sessions/' + sid).get_json()
            if data['status'] in statuses:
                return data
            time.sleep(.02)
        self.fail(f'会话没有进入 {statuses}: {data}')

    def test_health_and_examples(self):
        self.assertTrue(self.client.get('/api/health').get_json()['ok'])
        examples = self.client.get('/api/examples').get_json()['examples']
        self.assertGreaterEqual(len(examples), 6)
        for example in examples:
            compile(example['code'], example['id'], 'exec')

    def test_ast_facts_and_syntax_error(self):
        data = self.client.post('/api/analyze', json={'code': 'def f(x):\n    if x:\n        return x\nfor i in range(3):\n    f(i)'}).get_json()
        self.assertEqual(data['counts']['functions'], 1)
        self.assertEqual(data['counts']['branches'], 1)
        self.assertEqual(data['counts']['loops'], 1)
        self.assertEqual(data['tree']['label'], '程序')
        response = self.client.post('/api/analyze', json={'code': 'if !!!'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['line'], 1)

    def test_bad_inputs_and_cross_origin(self):
        for code in ['', None, 'a' * 30_001]:
            self.assertEqual(self.client.post('/api/sessions', json={'code': code}).status_code, 400)
        self.assertEqual(self.client.post('/api/sessions', json={'code': 'x=1', 'breakpoints': ['2']}).status_code, 400)
        self.assertEqual(self.client.post('/api/analyze', json={'code': 'x=1'}, headers={'Origin': 'https://example.com'}).status_code, 403)
        self.assertEqual(self.client.get('/api/health', headers={'Host': 'evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/analyze', data='x=1').status_code, 415)

    def test_real_debugging_history_and_export(self):
        response = self.client.post('/api/sessions', json={'code': 'x=2\nx=x+3\nprint("结果", x)', 'breakpoints': [3]})
        self.assertEqual(response.status_code, 201)
        sid = response.get_json()['id']
        initial = self.wait(sid, {'paused'})
        self.assertEqual(initial['events'][-1]['line'], 1)
        self.client.post(f'/api/sessions/{sid}/command', json={'command': 'run', 'breakpoints': [3]})
        paused = self.wait(sid, {'paused'})
        self.assertEqual(paused['events'][-1]['line'], 3)
        self.assertEqual(paused['events'][-1]['globals']['x']['text'], '5')
        after = paused['events'][-1]['index']
        self.client.post(f'/api/sessions/{sid}/command', json={'command': 'run', 'breakpoints': []})
        completed = self.wait(sid, {'completed'})
        self.assertIn('结果 5', completed['events'][-1]['output'])
        delta = self.client.get(f'/api/sessions/{sid}?after={after}').get_json()
        self.assertTrue(all(event['index'] > after for event in delta['events']))
        history = self.client.get('/api/history').get_json()['history']
        self.assertEqual(history[0]['id'], sid)
        exported = self.client.get(f'/api/sessions/{sid}/export')
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(json.loads(exported.data)['format'], 'pyscope-trace-v1')
        self.assertEqual(self.client.get(f'/api/history/{sid}').get_json()['status'], 'completed')
        self.client.delete(f'/api/history/{sid}')
        self.assertEqual(self.client.get('/api/history').get_json()['history'], [])

    def test_unicode_output_and_exception(self):
        sid = self.client.post('/api/sessions', json={'code': '名字="张三"\nprint(名字)\nanswer=1/0'}).get_json()['id']
        self.wait(sid, {'paused'})
        self.client.post(f'/api/sessions/{sid}/command', json={'command': 'run'})
        state = self.wait(sid, {'error'})
        self.assertTrue(any(e['event'] == 'exception' for e in state['events']))
        self.assertIn('张三', state['events'][-1]['output'])

    def test_profile_measures_real_functions(self):
        code = 'def square(n):\n    return n*n\nresult=[square(i) for i in range(12)]\nprint(sum(result))'
        data = self.client.post('/api/profile', json={'code': code}).get_json()
        self.assertEqual(data['exception'], None)
        self.assertIn('506', data['output'])
        rows = {row['name']: row for row in data['functions']}
        self.assertEqual(rows['square']['calls'], 12)
        self.assertGreaterEqual(data['elapsed_ms'], 0)
        error = self.client.post('/api/profile', json={'code': '1/0'}).get_json()
        self.assertIn('ZeroDivisionError', error['exception'])

    def test_profile_timeout(self):
        response = self.client.post('/api/profile', json={'code': 'while True:\n    pass'})
        self.assertEqual(response.status_code, 408)

    def test_unknown_session(self):
        self.assertEqual(self.client.get('/api/sessions/missing').status_code, 404)

    def test_history_without_browser_polling_and_deletion(self):
        sid = self.client.post('/api/sessions', json={'code': 'import time\ntime.sleep(.2)\nprint("done")'}).get_json()['id']
        self.wait(sid, {'paused'})
        self.client.post(f'/api/sessions/{sid}/command', json={'command': 'run'})
        # 完成后不再查询会话；后台收尾仍应持久化。
        time.sleep(.65)
        self.assertEqual(self.client.get('/api/history').get_json()['history'][0]['id'], sid)
        self.client.delete(f'/api/history/{sid}')
        self.client.get('/api/sessions/' + sid)
        time.sleep(.25)
        self.assertEqual(self.client.get('/api/history').get_json()['history'], [])

    def test_command_type_and_broken_exception_string(self):
        for value in [[], {}, 9, None]:
            self.assertEqual(self.client.post('/api/sessions/unknown/command', json={'command': value}).status_code, 400)
        result = self.client.post('/api/profile', json={'code': 'class Bad(Exception):\n    def __str__(self):\n        raise RuntimeError("broken")\nraise Bad()'}).get_json()
        self.assertIn('Bad:', result['exception'])


if __name__ == '__main__':
    unittest.main()
