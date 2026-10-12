"""可复现的独立性能实验：python tests/benchmark.py --output results.json。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from examples import EXAMPLES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='benchmark_results.json')
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    results = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
               'platform': platform.platform(), 'repeats': args.repeats,
               'measurement': '包含 cProfile 统计开销的代码执行时间，不包含进程启动时间；机器环境会影响耗时。', 'cases': []}
    with tempfile.TemporaryDirectory() as temp:
        app = create_app(data_dir=temp)
        client = app.test_client()
        try:
            for name in ('profile_slow', 'profile_fast'):
                example = next(item for item in EXAMPLES if item['id'] == name)
                runs = [client.post('/api/profile', json={'code': example['code']}).get_json() for _ in range(args.repeats)]
                if any(run.get('exception') or 'error' in run for run in runs):
                    raise RuntimeError(runs)
                function = next(row for row in runs[0]['functions'] if row['name'] == 'fibonacci')
                times = [run['elapsed_ms'] for run in runs]
                results['cases'].append({'id': name, 'title': example['title'], 'output': runs[0]['output'].strip(),
                                         'function_calls': function['calls'], 'median_ms': statistics.median(times), 'runs_ms': times})
        finally:
            app.extensions['shutdown']()
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
