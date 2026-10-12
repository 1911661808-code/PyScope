"""执行边界：本地可信代码，或每次运行独立的受限 Docker 容器。

Docker 共享宿主机内核；本模块不把容器描述成绝对安全的沙箱。
容器中的协议仍是不可信输入，必须在宿主侧限制长度并验证结构。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
MAX_LINE_CHARS = 2_000_000
MAX_TRACE_CHARS = 32_000_000
MAX_STDERR_CHARS = 64_000
MAX_RESULT_CHARS = 2_000_000

# 在真实容器内读取内核状态，不能仅凭 docker inspect 的请求配置断言限额有效。
PROBE_SOURCE = r'''
import json, os, pathlib, sys
sys.stdin.readline()
base = pathlib.Path('/sys/fs/cgroup')
def read(name):
    path = base / name
    return path.read_text().strip() if path.is_file() else None
status = dict(line.split(':', 1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
mounts = pathlib.Path('/proc/self/mountinfo').read_text().splitlines()
root = next(line.split()[5] for line in mounts if line.split()[4] == '/')
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'root_readonly': 'ro' in root.split(','),
  'networks': sorted(os.listdir('/sys/class/net')), 'no_new_privs': status.get('NoNewPrivs', '').strip(),
  'cap_effective': status.get('CapEff', '').strip(), 'memory': read('memory.max'),
  'swap': read('memory.swap.max'), 'pids': read('pids.max'), 'cpu': read('cpu.max')}))
'''


class BackendError(ValueError):
    """后端配置、启动或工作进程失败。"""


class ExecutionTimeout(BackendError):
    """运行时间达到限制。"""


class ProtocolError(BackendError):
    """工作进程返回不符合协议的内容。"""


class OutputLimitError(ProtocolError):
    """工作进程输出达到宿主侧限制。"""


def bounded_lines(stream, *, line_limit=MAX_LINE_CHARS, total_limit=MAX_TRACE_CHARS):
    """readline 的 size 参数同时约束无换行输出，避免先完整读入再截断。"""
    total = 0
    while True:
        line = stream.readline(min(line_limit, total_limit - total) + 1)
        if not line:
            return
        total += len(line)
        if len(line) > line_limit or total > total_limit:
            raise OutputLimitError("执行进程输出超过通信限制，已终止运行。")
        yield line


def parse_json(text):
    try:
        value = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("执行进程返回了无效 JSON。") from exc
    if type(value) is not dict:
        raise ProtocolError("执行进程消息必须是对象。")
    return value


def _text(value, limit=2000):
    if type(value) is not str or len(value) > limit:
        raise ProtocolError("执行进程的文本字段不符合协议。")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProtocolError("执行进程文本包含无效 Unicode。") from exc
    return value


def _integer(value, maximum=1_000_000_000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError("执行进程的整数字段不符合协议。")
    return value


def _number(value):
    if type(value) not in (int, float) or not 0 <= value <= 1e15 or not math.isfinite(value):
        raise ProtocolError("执行进程的数值字段不符合协议。")
    return value


def _list(value, maximum):
    if type(value) is not list or len(value) > maximum:
        raise ProtocolError("执行进程的列表字段不符合协议。")
    return value


def _dict(value):
    if type(value) is not dict:
        raise ProtocolError("执行进程的对象字段不符合协议。")
    return value


def validate_trace_message(message, limits):
    """复制白名单字段，避免伪造协议内容进入事件缓存或前端。"""
    _dict(message)
    if message.get("kind") == "status":
        status = message.get("status")
        if type(status) is not str or status not in {"starting", "running", "paused", "completed", "error", "stopped", "limited"}:
            raise ProtocolError("执行进程状态不符合协议。")
        error = message.get("error")
        if error is not None:
            _text(error, 4096)
        if "output" in message:
            _text(message["output"], limits["max_output"])
        return {"kind": "status", "status": status, "error": error}
    if message.get("kind") != "event":
        raise ProtocolError("执行进程消息类型不符合协议。")
    event = _dict(message.get("event"))
    if type(event.get("event")) is not str or event["event"] not in {"line", "call", "return", "exception"}:
        raise ProtocolError("执行事件类型不符合协议。")

    def value(item):
        _dict(item)
        kind = item.get("kind")
        if type(kind) is not str or kind not in {"value", "ref"}:
            raise ProtocolError("变量类型不符合协议。")
        result = {"kind": kind, "type": _text(item.get("type"), 256), "text": _text(item.get("text"), 4096)}
        if kind == "ref":
            result["id"] = _text(item.get("id"), 64)
        return result

    def namespace(items):
        _dict(items)
        if len(items) > limits["max_items"] + 1:
            raise ProtocolError("变量数量超过协议限制。")
        return {_text(key, 256): value(item) for key, item in items.items()}

    result = {"index": 0, "event": event["event"], "line": _integer(event.get("line")),
              "function": _text(event.get("function"), 256), "depth": _integer(event.get("depth"), limits["max_stack_depth"]),
              "output": _text(event.get("output"), limits["max_output"]), "elapsed_ms": _number(event.get("elapsed_ms")),
              "globals": namespace(event.get("globals")), "stack": [], "objects": []}
    for frame in _list(event.get("stack"), limits["max_stack_depth"]):
        _dict(frame)
        result["stack"].append({"name": _text(frame.get("name"), 256), "line": _integer(frame.get("line")),
                                "locals": namespace(frame.get("locals"))})
    for obj in _list(event.get("objects"), limits["max_objects"]):
        _dict(obj)
        record = {"id": _text(obj.get("id"), 64), "type": _text(obj.get("type"), 256),
                  "preview": _text(obj.get("preview"), 4096), "items": []}
        for entry in _list(obj.get("items"), limits["max_items"] + 1):
            _dict(entry)
            record["items"].append({"label": _text(entry.get("label"), 4096), "value": value(entry.get("value"))})
        result["objects"].append(record)
    if "return_value" in event:
        result["return_value"] = value(event["return_value"])
    if "exception" in event:
        result["exception"] = _text(event["exception"], 4096)
    return {"kind": "event", "event": result}


def _validate_profile(result):
    _dict(result)
    output = {"elapsed_ms": _number(result.get("elapsed_ms")), "output": _text(result.get("output"), 17000),
              "exception": result.get("exception"), "functions": []}
    if output["exception"] is not None:
        _text(output["exception"], 4096)
    for row in _list(result.get("functions"), 50):
        _dict(row)
        output["functions"].append({"name": _text(row.get("name"), 256), "line": _integer(row.get("line")),
            "calls": _integer(row.get("calls")), "primitive_calls": _integer(row.get("primitive_calls")),
            "total_ms": _number(row.get("total_ms")), "cumulative_ms": _number(row.get("cumulative_ms"))})
    return output


def _validate_analysis(result):
    _dict(result)
    if result.get("kind") == "syntax_error":
        error = SyntaxError(_text(result.get("message")))
        error.lineno = _integer(result.get("line", 0))
        error.offset = _integer(result.get("offset", 0))
        raise error
    if result.get("kind") == "analysis_error":
        raise BackendError(_text(result.get("message")))
    count = [0]

    def node(item, depth=0):
        _dict(item)
        count[0] += 1
        if depth > 12 or count[0] > 350:
            raise ProtocolError("AST 结构超过协议限制。")
        return {"label": _text(item.get("label"), 512), "line": _integer(item.get("line")),
                "children": [node(child, depth + 1) for child in _list(item.get("children"), 350)]}

    counts = _dict(result.get("counts"))
    return {"tree": node(result.get("tree")),
            "counts": {key: _integer(counts.get(key)) for key in ("lines", "functions", "classes", "branches", "loops")},
            "functions": [{"name": _text(_dict(item).get("name"), 256), "line": _integer(item.get("line"))}
                          for item in _list(result.get("functions"), 10000)],
            "warnings": [_text(item) for item in _list(result.get("warnings"), 20)]}


def _popen(command, **kwargs):
    return subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def _collect_result(process, code, timeout):
    """并行有界读两个管道；不用 communicate 将全部不可信输出一次装入内存。"""
    parts, failures = [], []
    deadline = time.monotonic() + timeout

    def read(stream, output, limit):
        try:
            for line in bounded_lines(stream, line_limit=limit, total_limit=limit):
                if output is not None:
                    output.append(line)
        except (OSError, ValueError) as exc:
            failures.append(exc)
            process.kill()

    readers = [threading.Thread(target=read, args=(process.stdout, parts, MAX_RESULT_CHARS), daemon=True),
               threading.Thread(target=read, args=(process.stderr, None, MAX_STDERR_CHARS), daemon=True)]

    def write():
        try:
            process.stdin.write(json.dumps({"code": code}, ensure_ascii=True) + "\n")
            process.stdin.close()
        except (OSError, ValueError) as exc:
            failures.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    for thread in readers + [writer]:
        thread.start()
    try:
        try:
            result_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeout(f"执行超过 {timeout:g} 秒，已终止运行。") from exc
        for thread in readers + [writer]:
            thread.join(timeout=max(0.01, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in readers + [writer]):
            raise ExecutionTimeout("执行进程未及时关闭通信管道。")
        if failures:
            if isinstance(failures[0], ProtocolError):
                raise failures[0]
            raise BackendError("执行进程通信异常。")
        if result_code:
            raise BackendError("执行进程未正常完成。")
        return parse_json("".join(parts))
    finally:
        # 正常、超时、协议错误均进入清理；Docker wrapper 总会显式 rm -f。
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        for thread in readers + [writer]:
            thread.join(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class LocalBackend:
    """仅用于可信本地代码；保留 Windows 免安装程序的 worker 入口。"""
    mode = "local"

    def __init__(self, *, timeout_seconds=10):
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _command(worker):
        if getattr(sys, "frozen", False):
            return [sys.executable, f"--{worker.replace('_', '-')}"]
        return [sys.executable, "-I", "-X", "utf8", "-u", str(ROOT / f"{worker}.py")]

    def spawn_trace(self):
        return _popen(self._command("trace_worker"), env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})

    def run_profile(self, code):
        from analysis_engine import validate_code
        validate_code(code)
        compile(code, "<pyscope>", "exec")
        with tempfile.TemporaryDirectory(prefix="pyscope-profile-") as directory:
            return _validate_profile(_collect_result(_popen(self._command("profile_worker"), cwd=directory), code, self.timeout_seconds))

    @staticmethod
    def analyze(code):
        from analysis_engine import analyze
        return analyze(code)

    def close(self):
        pass


class _ContainerProcess:
    """终止 Docker CLI 不会终止容器，因此每个退出路径都显式删除容器。"""
    def __init__(self, backend, name, process):
        self.backend, self.name, self.process = backend, name, process
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self._lock = threading.Lock()
        self._removed = False

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        result = self.process.wait(timeout=timeout)
        self._remove()
        return result

    def _remove(self):
        with self._lock:
            if not self._removed:
                self.backend._remove(self.name)
                self._removed = True

    def terminate(self):
        try:
            self._remove()
        finally:
            if self.process.poll() is None:
                self.process.kill()

    kill = terminate


class DockerBackend:
    """固定本地镜像、每次创建独立容器；不会拉取镜像或降级到 LocalBackend。"""
    mode = "docker"

    def __init__(self, *, image="pyscope-worker:1.1.0", runtime=None, timeout_seconds=10,
                 startup_timeout=8, memory_mb=128, cpus=0.5, pids_limit=32, docker_command="docker"):
        if not isinstance(image, str) or not image or image.startswith("-"):
            raise BackendError("必须指定本地 worker 镜像。")
        if runtime is not None and (not isinstance(runtime, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", runtime)):
            raise BackendError("容器 runtime 名称无效。")
        if (type(memory_mb) is not int or memory_mb < 32 or type(pids_limit) is not int or pids_limit < 4
                or type(cpus) not in (int, float) or not math.isfinite(cpus) or cpus <= 0
                or timeout_seconds <= 0 or startup_timeout <= 0):
            raise BackendError("容器资源限制无效。")
        self.image, self.runtime = image, runtime
        self.timeout_seconds, self.startup_timeout = float(timeout_seconds), float(startup_timeout)
        self.memory_mb, self.cpus, self.pids_limit = memory_mb, cpus, pids_limit
        self.docker_command = docker_command
        self._containers, self._lock = set(), threading.RLock()
        self._closed = False
        self._verified = False
        self._rootless_verified = False
        self._startup_cleaned = False
        self._verify_lock = threading.Lock()
        self._docker_env = os.environ.copy()
        self._docker_env["LANG"] = "C.UTF-8"
        result = self._docker(["image", "inspect", "--format", "{{.Id}} {{.Os}}", image])
        match = re.fullmatch(r"(sha256:[0-9a-f]{64}) linux", result.stdout.strip())
        if result.returncode or not match:
            raise BackendError("隔离执行需要已经构建的本地 Linux worker 镜像；不会自动拉取或退回本地运行。")
        self.image_id = match.group(1)

    def _docker(self, arguments, *, timeout=None):
        try:
            return subprocess.run([self.docker_command, *arguments], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                timeout=timeout or self.startup_timeout, env=self._docker_env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError("无法连接 Docker 或 Docker 操作超时；隔离后端没有退回本地执行。") from exc

    def _create_command(self, name, worker):
        arguments = ["create", "--pull=never", "--rm", "--interactive", "--name", name,
            "--label", "pyscope.worker=true", "--network", "none", "--read-only", "--user", "65532:65532",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit", str(self.pids_limit),
            "--memory", f"{self.memory_mb}m", "--memory-swap", f"{self.memory_mb}m", "--cpus", str(self.cpus),
            "--ulimit", "nofile=64:64", "--ulimit", "core=0:0", "--ipc", "none", "--init",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--tmpfs", "/work:rw,noexec,nosuid,nodev,size=16m,mode=1777", "--workdir", "/work",
            "--entrypoint", "python3"]
        if self.runtime:
            arguments.extend(["--runtime", self.runtime])
        entry = ["-c", PROBE_SOURCE] if worker == "probe" else [f"/opt/pyscope/{worker}.py"]
        return [*arguments, self.image_id, "-I", "-B", "-u", *entry]

    def _remove(self, name):
        deadline = time.monotonic() + self.startup_timeout
        while True:
            result = self._docker(["rm", "--force", name], timeout=max(.05, deadline - time.monotonic()))
            if (result.returncode and "already in progress" in result.stderr.lower()
                    and time.monotonic() < deadline):
                # --rm 的自动清理可能与显式删除短暂相撞，等待同一删除操作结束。
                time.sleep(.02)
                continue
            break
        # --rm 正常退出后不存在是幂等成功；其他失败必须报告，不能宣称已清理。
        if result.returncode and "no such container" not in result.stderr.lower():
            raise BackendError("无法确认临时容器已清理，请检查 Docker 服务。")
        with self._lock:
            self._containers.discard(name)

    def _spawn(self, worker):
        with self._lock:
            if self._closed:
                raise BackendError("执行后端已关闭。")
            name = "pyscope-" + uuid.uuid4().hex
            self._containers.add(name)
        process = None
        try:
            created = self._docker(self._create_command(name, worker))
            if created.returncode:
                raise BackendError("无法创建受限执行容器，请检查镜像及容器运行配置。")
            process = _popen([self.docker_command, "start", "--attach", "--interactive", name], env=self._docker_env)
            wrapped = _ContainerProcess(self, name, process)
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                state = self._docker(["inspect", "--format", "{{.State.Running}}", name], timeout=max(.05, deadline - time.monotonic()))
                if state.returncode == 0 and state.stdout.strip() == "true":
                    if self._closed:
                        raise BackendError("执行后端已关闭。")
                    return wrapped
                if process.poll() is not None:
                    raise BackendError("隔离执行容器启动失败。")
                time.sleep(.02)
            raise ExecutionTimeout("隔离执行容器启动超时。")
        except BaseException:
            try:
                self._remove(name)
            finally:
                if process is not None:
                    process.kill()
                    process.wait(timeout=2)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
            raise

    def spawn_trace(self):
        self._ensure_verified()
        return self._spawn("trace_worker")

    def run_profile(self, code):
        from analysis_engine import validate_code
        validate_code(code)
        self._ensure_verified()
        return _validate_profile(_collect_result(self._spawn("profile_worker"), code, self.timeout_seconds))

    def analyze(self, code):
        from analysis_engine import validate_code
        validate_code(code)
        self._ensure_verified()
        return _validate_analysis(_collect_result(self._spawn("analysis_worker"), code, self.timeout_seconds))

    def _ensure_verified(self):
        if not self._verified:
            self.verify()

    def cleanup_orphans(self):
        """仅在确认专用 rootless daemon 后清理本应用标签的重启遗留容器。"""
        if not self._rootless_verified:
            raise BackendError("仅允许在已验证的专用 rootless Docker 服务中清理遗留容器。")
        result = self._docker(["ps", "--all", "--quiet", "--no-trunc", "--filter", "label=pyscope.worker=true"])
        if result.returncode:
            raise BackendError("无法枚举本应用的遗留容器。")
        identifiers = result.stdout.split()
        if any(not re.fullmatch(r"[0-9a-f]{12,64}", item) for item in identifiers):
            raise BackendError("Docker 返回了无效的遗留容器标识。")
        # 此方法只用于服务启动；运行中禁止调用，以免误删当前运行。
        with self._lock:
            if self._containers:
                raise BackendError("存在活跃运行时不能清理启动遗留容器。")
            for identifier in identifiers:
                self._remove(identifier)
        return len(identifiers)

    def verify(self, require_rootless=True):
        """启动时强制检验有效隔离和 cgroup v2 限额；不支持时拒绝运行。"""
        with self._verify_lock:
            if self._closed:
                raise BackendError("执行后端已关闭。")
            if require_rootless and not self._rootless_verified:
                info = self._docker(["info", "--format", "{{json .SecurityOptions}}"])
                try:
                    options = json.loads(info.stdout)
                except (ValueError, TypeError, RecursionError):
                    options = None
                if info.returncode or type(options) is not list or not any(type(item) is str and item.split(",")[0] == "name=rootless" for item in options):
                    raise BackendError("服务器执行必须连接专用 rootless Docker 服务，当前服务未通过验证。")
                self._rootless_verified = True
            if require_rootless and not self._startup_cleaned:
                self.cleanup_orphans()
                self._startup_cleaned = True
            if self._verified:
                return {"ok": True, "mode": self.mode, "image_id": self.image_id, "rootless": self._rootless_verified}
            result = _collect_result(self._spawn("probe"), "", self.startup_timeout)
            try:
                quota, period = map(int, result["cpu"].split())
                effective = (type(result["uid"]) is int and result["uid"] == 65532 and result["gid"] == 65532
                    and result["root_readonly"] is True and result["networks"] == ["lo"]
                    and result["no_new_privs"] == "1" and int(result["cap_effective"], 16) == 0
                    and 0 < int(result["memory"]) <= self.memory_mb * 1024 * 1024
                    and int(result["swap"]) == 0 and 0 < int(result["pids"]) <= self.pids_limit
                    and quota > 0 and period > 0 and quota / period <= self.cpus + 1e-6)
            except (KeyError, ValueError, TypeError, AttributeError):
                effective = False
            if not effective:
                raise BackendError("容器隔离或 cgroup v2 资源限制未生效，已拒绝执行。请检查内存、CPU、进程控制器和 rootless 委派配置。")
            self._verified = True
            return {"ok": True, "mode": self.mode, "image_id": self.image_id, "rootless": self._rootless_verified}

    def close(self):
        with self._lock:
            self._closed = True
            containers = tuple(self._containers)
        errors = []
        for name in containers:
            try:
                self._remove(name)
            except BackendError as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
