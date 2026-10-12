"""PyScope 独立调试进程。

标准输出上的 JSON 行仅供父进程通信，用户输出另行捕获。
本进程用于运行可信的本地代码，并不构成安全沙箱。
"""

from __future__ import annotations

import io
from itertools import islice
import json
import sys
import threading
import time
import types


USER_FILENAME = "<pyscope-user>"
PROTOCOL_OUT = sys.stdout
PROTOCOL_IN = sys.stdin
PROTOCOL_LOCK = threading.Lock()


def exception_text(exception: BaseException, exception_type=None) -> str:
    """异常对象也可能有自定义 __str__，诊断文本不应使调试器崩溃。"""
    name = (exception_type or type(exception)).__name__
    try:
        message = str(exception)
    except BaseException:
        message = "异常对象的文本表示不可用"
    if len(message) > 2000:
        message = message[:2000] + "…（异常信息已截断）"
    return f"{name}: {message}"


def send(payload: dict) -> None:
    """协议输出始终使用原始流，避免被 print 或重定向污染。"""
    with PROTOCOL_LOCK:
        PROTOCOL_OUT.write(json.dumps(payload, ensure_ascii=False) + "\n")
        PROTOCOL_OUT.flush()


class ExecutionStopped(BaseException):
    """控制器请求停止，不作为用户程序异常显示。"""


class ExecutionLimited(BaseException):
    """达到资源限制，通常的 except Exception 不会吞掉此信号。"""


class CapturedOutput(io.TextIOBase):
    """保存有限的累积输出，每个事件都带上当时的输出快照。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("write() 仅接受字符串")
        remaining = max(0, self.limit - self.length)
        if len(value) <= remaining:
            self.parts.append(value)
            self.length += len(value)
            return len(value)
        self.parts.append(value[:remaining])
        self.length = self.limit
        self.truncated = True
        raise ExecutionLimited(f"输出超过 {self.limit} 个字符，已停止执行。")

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        # 合并后缓存，避免长循环产生大量很小的字符串片段。
        value = "".join(self.parts)
        self.parts = [value] if value else []
        return value


class ObjectRegistry:
    """会话内保留有限的强引用，防止 Python 回收后复用 id。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.objects: dict[int, tuple[str, object]] = {}

    def identify(self, value: object) -> str | None:
        address = id(value)
        old = self.objects.get(address)
        if old is not None:
            return old[0]
        if len(self.objects) >= self.limit:
            return None
        name = f"o{len(self.objects) + 1}"
        self.objects[address] = (name, value)
        return name


class Snapshot:
    """生成 JSON 可序列化快照，不调用用户对象的自定义 repr。"""

    def __init__(self, registry: ObjectRegistry, limits: dict):
        self.registry = registry
        self.max_depth = limits["max_depth"]
        self.max_items = limits["max_items"]
        self.max_objects = limits["max_objects"]
        self.objects: list[dict] = []
        self.seen: set[str] = set()

    @staticmethod
    def scalar(type_name: str, text: str) -> dict:
        return {"kind": "value", "type": type_name, "text": text}

    @staticmethod
    def clipped(value: str, size: int = 180) -> str:
        return value if len(value) <= size else value[:size] + "…"

    def encode(self, value: object, depth: int = 0) -> dict:
        value_type = type(value)
        type_name = value_type.__name__
        if value is None or value_type in (bool, int, float, complex, str, bytes):
            try:
                text = repr(value)
            except (ValueError, OverflowError):
                text = f"<{type_name}：数值过长>"
            return self.scalar(type_name, self.clipped(text))
        if value_type is types.FunctionType:
            return self.scalar(type_name, f"<function {value.__qualname__}>")
        if value_type in (types.BuiltinFunctionType, types.BuiltinMethodType):
            return self.scalar(type_name, f"<builtin {value.__name__}>")
        if value_type is types.MethodType:
            return self.scalar(type_name, f"<method {value.__func__.__qualname__}>")
        if value_type is types.ModuleType:
            return self.scalar(type_name, f"<module {value.__name__}>")
        if isinstance(value, type):
            return self.scalar(type_name, f"<class {value.__name__}>")
        if value_type is range:
            return self.scalar(type_name, self.clipped(repr(value)))

        object_id = self.registry.identify(value)
        if object_id is None:
            return self.scalar(type_name, f"<{type_name}：会话对象数上限>")
        preview = type_name
        if value_type in (list, tuple, dict, set, frozenset):
            preview = f"{type_name} ({len(value)})"
        reference = {"kind": "ref", "type": type_name, "id": object_id, "text": preview}
        if object_id in self.seen:
            return reference
        if len(self.objects) >= self.max_objects:
            return self.scalar(type_name, f"<{type_name}：单帧对象数上限>")

        self.seen.add(object_id)
        record = {"id": object_id, "type": type_name, "preview": preview, "items": []}
        self.objects.append(record)
        if depth >= self.max_depth:
            record["items"].append({"label": "…", "value": self.scalar("limit", "达到展开深度上限")})
            return reference

        try:
            if value_type in (list, tuple):
                entries = [(f"[{i}]", item) for i, item in enumerate(value[:self.max_items])]
                size = len(value)
            elif value_type is dict:
                entries = []
                for i, (key, item) in enumerate(value.items()):
                    if i >= self.max_items:
                        break
                    # 复杂键只展示类型，避免 repr 执行用户逻辑。
                    label = self.encode_key(key)
                    entries.append((label, item))
                size = len(value)
            elif value_type in (set, frozenset):
                # 按本次运行中的对象地址排序，避免异类元素互相比较。
                values = sorted(islice(value, self.max_items), key=id)
                entries = [(f"{{{i}}}", item) for i, item in enumerate(values)]
                size = len(value)
            else:
                try:
                    attributes = object.__getattribute__(value, "__dict__")
                except (AttributeError, TypeError):
                    attributes = {}
                if type(attributes) is not dict:
                    attributes = {}
                entries = [(str(key), item) for key, item in islice(attributes.items(), self.max_items)]
                size = len(attributes)
                if not attributes:
                    record["items"].append({"label": "说明", "value": self.scalar("info", "此类型无可展示的实例属性")})
            for label, item in entries:
                record["items"].append({"label": label, "value": self.encode(item, depth + 1)})
            if size > self.max_items:
                record["items"].append({"label": "…", "value": self.scalar("limit", f"另有 {size - self.max_items} 项未展开")})
        except Exception as exc:
            record["items"].append({"label": "说明", "value": self.scalar("info", f"无法读取属性：{type(exc).__name__}")})
        return reference

    def encode_key(self, key: object) -> str:
        if type(key) in (str, int, float, bool, bytes, type(None)):
            try:
                return self.clipped(repr(key), 80)
            except (ValueError, OverflowError):
                pass
        return f"<{type(key).__name__} 键>"

    def namespace(self, values: dict) -> dict:
        result = {}
        for name, value in values.items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if len(result) >= self.max_items:
                result["…"] = self.scalar("limit", "其余变量未展开")
                break
            result[name] = self.encode(value)
        return result


class DebugController:
    """命令线程只修改控制状态，真正的暂停发生在被调试线程。"""

    def __init__(self, breakpoints: list[int]):
        self.condition = threading.Condition()
        self.breakpoints = set(breakpoints)
        self.mode = "step"
        self.pause_requested = False
        self.stop_requested = False
        self.resume_requested = False
        self.paused = False
        self.paused_duration = 0.0

    def listen(self) -> None:
        try:
            for raw in PROTOCOL_IN:
                try:
                    command = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                with self.condition:
                    if "breakpoints" in command:
                        self.breakpoints = set(command["breakpoints"])
                    action = command.get("command")
                    if action == "stop":
                        self.stop_requested = True
                    elif action == "pause":
                        self.pause_requested = True
                    elif action in ("step", "run"):
                        self.mode = action
                        self.pause_requested = False
                        self.resume_requested = True
                    self.condition.notify_all()
        finally:
            # 父进程退出或关闭管道后，不留下被暂停的孤立进程。
            with self.condition:
                self.stop_requested = True
                self.condition.notify_all()

    def check_stop(self) -> None:
        if self.stop_requested:
            raise ExecutionStopped()

    def line_checkpoint(self, line: int) -> None:
        with self.condition:
            self.check_stop()
            if self.mode != "step" and not self.pause_requested and line not in self.breakpoints:
                return
            self.paused = True
            self.resume_requested = False
            start = time.perf_counter()
            send({"kind": "status", "status": "paused"})
            try:
                while not self.resume_requested and not self.stop_requested:
                    self.condition.wait()
                self.check_stop()
            finally:
                self.paused_duration += time.perf_counter() - start
                self.paused = False
            send({"kind": "status", "status": "running"})


class Tracer:
    def __init__(self, control: DebugController, output: CapturedOutput, limits: dict):
        self.control = control
        self.output = output
        self.limits = limits
        self.registry = ObjectRegistry(limits["max_total_objects"])
        self.started = time.perf_counter()
        self.event_count = 0
        self.last_frame = None

    def emit_event(self, frame, event: str, arg=None) -> None:
        frames = []
        current = frame
        while current is not None:
            if current.f_code.co_filename == USER_FILENAME:
                frames.append(current)
            current = current.f_back
        frames.reverse()
        if len(frames) > self.limits["max_stack_depth"]:
            raise ExecutionLimited(f"调用栈超过 {self.limits['max_stack_depth']} 层，已停止执行。")
        snapshot = Snapshot(self.registry, self.limits)
        stack = [
            {"name": item.f_code.co_name, "line": item.f_lineno, "locals": snapshot.namespace(item.f_locals)}
            for item in frames
        ]
        payload = {
            "index": self.event_count,
            "event": event,
            "line": frame.f_lineno,
            "function": frame.f_code.co_name,
            "depth": max(0, len(frames) - 1),
            "stack": stack,
            "globals": snapshot.namespace(frame.f_globals),
            "objects": snapshot.objects,
            "output": self.output.getvalue(),
            "elapsed_ms": round((time.perf_counter() - self.started - self.control.paused_duration) * 1000, 3),
        }
        if event == "return":
            payload["return_value"] = snapshot.encode(arg)
        elif event == "exception":
            exception_type, exception, _ = arg
            payload["exception"] = exception_text(exception, exception_type)
        send({"kind": "event", "event": payload})
        self.event_count += 1

    def __call__(self, frame, event: str, arg):
        if frame.f_code.co_filename != USER_FILENAME:
            return None
        self.control.check_stop()
        if event not in ("call", "line", "return", "exception"):
            return self
        self.last_frame = frame
        if self.event_count >= self.limits["max_events"]:
            raise ExecutionLimited(f"执行事件达到 {self.limits['max_events']} 条上限，已停止执行。")
        self.emit_event(frame, event, arg)
        if event == "line":
            self.control.line_checkpoint(frame.f_lineno)
        return self


def main() -> None:
    request = json.loads(PROTOCOL_IN.readline())
    limits = request["limits"]
    output = CapturedOutput(limits["max_output"])
    control = DebugController(request.get("breakpoints", []))
    tracer = Tracer(control, output, limits)
    command_thread = threading.Thread(target=control.listen, name="pyscope-command", daemon=True)
    command_thread.start()
    sys.stdout = output
    sys.stderr = output
    # input() 暂不支持，避免与调试命令争用标准输入。
    sys.stdin = io.StringIO("")
    status, error = "completed", None
    try:
        code = compile(request["code"], USER_FILENAME, "exec")
        namespace = {"__name__": "__main__", "__file__": USER_FILENAME}
        send({"kind": "status", "status": "running"})
        sys.settrace(tracer)
        exec(code, namespace, namespace)
    except ExecutionStopped:
        status = "stopped"
    except ExecutionLimited as exc:
        status, error = "limited", str(exc)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            status, error = "error", exception_text(exc)
    except BaseException as exc:
        status, error = "error", exception_text(exc)
        if isinstance(exc, SyntaxError):
            error = f"SyntaxError: 第 {exc.lineno} 行，{exc.msg}"
    finally:
        sys.settrace(None)
        send({"kind": "status", "status": status, "error": error, "output": output.getvalue()})
        # 父进程收到终态后关闭输入管道，让命令线程在解释器退出前离开读取。
        command_thread.join(timeout=2.0)
        sys.stdout = PROTOCOL_OUT
        sys.stdin = PROTOCOL_IN


if __name__ == "__main__":
    main()
