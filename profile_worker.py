"""在单独进程中测量用户代码，不把调试快照开销计入分析。"""
import cProfile
import contextlib
import io
import json
import pstats
import sys
import time


class CappedOutput(io.TextIOBase):
    def __init__(self, limit=16_000):
        self.limit, self.parts, self.length = limit, [], 0
        self.truncated = False

    def write(self, text):
        text = str(text)
        available = self.limit - self.length
        if available > 0:
            self.parts.append(text[:available])
            self.length += min(len(text), available)
        if len(text) > available:
            self.truncated = True
        return len(text)

    def flush(self):
        pass

    def getvalue(self):
        return ''.join(self.parts) + ('\n[输出已截断]' if self.truncated else '')


def main():
    payload = json.loads(sys.stdin.readline())
    source = compile(payload['code'], '<pyscope>', 'exec')
    output, profiler = CappedOutput(), cProfile.Profile()
    namespace = {'__name__': '__main__', '__file__': '<pyscope>'}
    def unsupported_input(prompt=''):
        raise RuntimeError('当前版本不支持交互 input()，请在代码中直接设置输入。')
    namespace['input'] = unsupported_input
    error = None
    start = time.perf_counter()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            profiler.enable()
            exec(source, namespace, namespace)
        except BaseException as exc:
            try:
                detail = str(exc)[:2000]
            except BaseException:
                detail = '异常对象无法转换为文本'
            error = f'{type(exc).__name__}: {detail}'
        finally:
            profiler.disable()
    elapsed = (time.perf_counter() - start) * 1000
    rows = []
    for (filename, line, name), (primitive, calls, total, cumulative, callers) in pstats.Stats(profiler).stats.items():
        if filename == '<pyscope>' and name != '<module>':
            rows.append({'name': name, 'line': line, 'calls': calls,
                         'primitive_calls': primitive, 'total_ms': round(total * 1000, 6),
                         'cumulative_ms': round(cumulative * 1000, 6)})
    rows.sort(key=lambda row: row['cumulative_ms'], reverse=True)
    sys.__stdout__.write(json.dumps({'elapsed_ms': round(elapsed, 6), 'functions': rows[:50],
                                   'output': output.getvalue(), 'exception': error}, ensure_ascii=True))


if __name__ == '__main__':
    main()
