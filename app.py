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

from flask import Flask, jsonify, render_template, request, send_file
from analysis_engine import analyze, validate_code
from examples import EXAMPLES
from session_manager import SessionManager

ROOT = Path(__file__).resolve().parent
TERMINAL = {'completed', 'error', 'stopped', 'limited'}


def create_app(data_dir=None, manager=None):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = 150_000
    app.json.ensure_ascii = False
    sessions = manager or SessionManager()
    app.extensions['sessions'] = sessions
    directory = Path(data_dir or os.environ.get('PYSCOPE_DATA_DIR', ROOT / 'data'))
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / 'history.sqlite3'
    lock = threading.Lock()
    persisted = set()
    pending = set()
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
                conn.execute('INSERT OR IGNORE INTO history VALUES (?,?,?,?,?,?)',
                             (state['id'], title, complete['status'], complete['total_events'],
                              str(complete.get('created_at', '')), serialized))
                conn.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY rowid DESC LIMIT 100)')
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
        return render_template('index.html')

    @app.get('/api/health')
    def health():
        return jsonify(ok=True, python=sys.version.split()[0], mode='local')

    @app.get('/api/examples')
    def examples():
        return jsonify(examples=EXAMPLES)

    @app.post('/api/analyze')
    def structure():
        return jsonify(analyze(body().get('code')))

    @app.post('/api/sessions')
    def start():
        data = body()
        code = validate_code(data.get('code'))
        compile(code, '<pyscope>', 'exec')
        sid = sessions.start(code, checked_breakpoints(data.get('breakpoints', [])))
        with lock:
            pending.add(sid)
        return jsonify(sessions.get(sid, -1)), 201

    @app.get('/api/sessions/<sid>')
    def state(sid):
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
        points = checked_breakpoints(data['breakpoints']) if 'breakpoints' in data else None
        result = sessions.command(sid, data.get('command'), points)
        persist(result)
        return jsonify(result)

    @app.get('/api/sessions/<sid>/export')
    def export(sid):
        try:
            result = sessions.get(sid, -1)
        except KeyError:
            with connection() as conn:
                record = conn.execute('SELECT payload FROM history WHERE id=?', (sid,)).fetchone()
            if not record:
                raise KeyError(sid)
            result = json.loads(record[0])
        payload = json.dumps({'format': 'pyscope-trace-v1', **result}, ensure_ascii=False, indent=2).encode('utf-8')
        return send_file(io.BytesIO(payload), mimetype='application/json', as_attachment=True,
                         download_name=f'pyscope-{sid[:8]}.json')

    @app.post('/api/profile')
    def profile():
        code = validate_code(body().get('code'))
        compile(code, '<pyscope>', 'exec')
        with tempfile.TemporaryDirectory(prefix='pyscope-profile-') as temp:
            try:
                worker_command = ([sys.executable, '--profile-worker'] if getattr(sys, 'frozen', False)
                                  else [sys.executable, '-I', str(ROOT / 'profile_worker.py')])
                completed = subprocess.run(worker_command,
                    input=json.dumps({'code': code}), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding='utf-8', errors='replace', cwd=temp, timeout=10,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except subprocess.TimeoutExpired:
                return jsonify(error='性能分析超过 10 秒，已终止该运行。'), 408
        if completed.returncode or not completed.stdout.strip():
            return jsonify(error='性能分析进程未正常完成。'), 422
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return jsonify(error='性能分析结果格式异常。'), 422
        return jsonify(result)

    @app.get('/api/history')
    def history():
        with connection() as conn:
            rows = conn.execute('SELECT id,title,status,event_count,created_at FROM history ORDER BY rowid DESC LIMIT 100').fetchall()
        return jsonify(history=[dict(zip(('id','title','status','event_count','created_at'), row)) for row in rows])

    @app.get('/api/history/<sid>')
    def history_item(sid):
        with connection() as conn:
            row = conn.execute('SELECT payload FROM history WHERE id=?', (sid,)).fetchone()
        if not row:
            raise KeyError(sid)
        return jsonify(json.loads(row[0]))

    @app.delete('/api/history/<sid>')
    def history_delete(sid):
        with lock, connection() as conn:
            conn.execute('DELETE FROM history WHERE id=?', (sid,))
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
