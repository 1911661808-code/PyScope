"""隔离容器内的 AST 入口；宿主机不解析不可信代码。"""
import json
import sys

# -I 不把脚本目录加入导入路径；只添加镜像内固定的只读应用目录。
sys.path.insert(0, "/opt/pyscope")
from analysis_engine import analyze


def main():
    try:
        payload = json.loads(sys.stdin.readline(600_001))
        result = analyze(payload["code"])
    except SyntaxError as exc:
        result = {"kind": "syntax_error", "message": str(exc.msg)[:2000], "line": exc.lineno or 0, "offset": exc.offset or 0}
    except (ValueError, TypeError, KeyError, RecursionError, MemoryError):
        result = {"kind": "analysis_error", "message": "无法分析此代码，可能超过结构或资源限制。"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
