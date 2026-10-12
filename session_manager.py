"""调试会话生命周期与进程通信。

执行时间不包含用户查看暂停快照的时间。看门狗可以终止卡在原生调用
中的进程；这些资源限制不等于安全沙箱，仅运行可信的本地代码。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid


TERMINAL_STATUSES = frozenset({"completed", "error", "stopped", "limited"})
ALL_STATUSES = TERMINAL_STATUSES | {"starting", "running", "paused"}


@dataclass
class _Session:
    id: str
    code: str
    breakpoints: list[int]
    process: subprocess.Popen
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "starting"
    events: list[dict] = field(default_factory=list)
    error: str | None = None
    diagnostic: str = ""
    born: float = field(default_factory=time.monotonic)
    active_started: float | None = field(default_factory=time.monotonic)
    active_seconds: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock)
    write_lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None


class SessionManager:
    """线程安全的本地会话管理器，可通过构造参数调整教学演示限制。"""

    def __init__(
        self,
        *,
        max_events: int = 1500,
        max_output: int = 16000,
        max_depth: int = 5,
        max_items: int = 30,
        max_objects: int = 120,
        max_total_objects: int = 6000,
        max_stack_depth: int = 80,
        timeout_seconds: float = 30.0,
        max_lifetime_seconds: float = 1800.0,
        max_sessions: int = 8,
        max_saved_sessions: int = 100,
    ):
        integer_limits = {
            "max_events": max_events,
            "max_output": max_output,
            "max_depth": max_depth,
            "max_items": max_items,
            "max_objects": max_objects,
            "max_total_objects": max_total_objects,
            "max_stack_depth": max_stack_depth,
        }
        if any(type(value) is not int or value < 1 for value in integer_limits.values()):
            raise ValueError("资源限制必须为正整数")
        if timeout_seconds <= 0 or max_lifetime_seconds <= 0 or max_sessions < 1 or max_saved_sessions < 1:
            raise ValueError("时间和会话数量限制必须为正数")
        self._limits = integer_limits
        self.timeout_seconds = float(timeout_seconds)
        self.max_lifetime_seconds = float(max_lifetime_seconds)
        self.max_sessions = int(max_sessions)
        self.max_saved_sessions = int(max_saved_sessions)
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._watcher = threading.Thread(target=self._watchdog, name="pyscope-watchdog", daemon=True)
        self._watcher.start()

    @staticmethod
    def _validate_breakpoints(breakpoints) -> list[int]:
        if breakpoints is None:
            return []
        if not isinstance(breakpoints, (list, tuple, set)):
            raise ValueError("断点必须为行号列表")
        if any(type(line) is not int or line < 1 for line in breakpoints):
            raise ValueError("断点行号必须为正整数")
        return sorted(set(breakpoints))

    def start(self, code: str, breakpoints=None) -> str:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("代码不能为空")
        if len(code) > 100000:
            raise ValueError("代码长度不能超过 100000 个字符")
        points = self._validate_breakpoints(breakpoints)
        with self._lock:
            if self._closed.is_set():
                raise ValueError("调试管理器已关闭")
            active = sum(item.status not in TERMINAL_STATUSES for item in self._sessions.values())
            if active >= self.max_sessions:
                raise ValueError(f"同时运行的会话不能超过 {self.max_sessions} 个，请先停止旧会话")
            # 已结束的内存会话按创建顺序淘汰；持久化历史由应用层负责。
            terminal = [key for key, item in self._sessions.items() if item.status in TERMINAL_STATUSES]
            while len(terminal) >= self.max_saved_sessions:
                self._sessions.pop(terminal.pop(0))
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            worker_command = (
                [sys.executable, "--trace-worker"]
                if getattr(sys, "frozen", False)
                else [sys.executable, "-u", str(Path(__file__).with_name("trace_worker.py"))]
            )
            process = subprocess.Popen(
                worker_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=flags,
            )
            session_id = uuid.uuid4().hex
            session = _Session(id=session_id, code=code, breakpoints=points, process=process)
            self._sessions[session_id] = session
            session.reader = threading.Thread(target=self._read_events, args=(session,), name=f"pyscope-read-{session_id[:6]}", daemon=True)
            session.stderr_reader = threading.Thread(target=self._read_stderr, args=(session,), name=f"pyscope-stderr-{session_id[:6]}", daemon=True)
            session.reader.start()
            session.stderr_reader.start()
            try:
                self._write(session, {"code": code, "breakpoints": points, "limits": self._limits})
            except (OSError, ValueError) as exc:
                self._terminate(session, "error", f"无法启动调试进程：{exc}")
            return session_id

    def _find(self, session_id: str) -> _Session:
        with self._lock:
            return self._sessions[session_id]

    @staticmethod
    def _state_locked(session: _Session, after: int = -1) -> dict:
        return {
            "id": session.id,
            "status": session.status,
            "code": session.code,
            "breakpoints": list(session.breakpoints),
            "events": copy.deepcopy([event for event in session.events if event["index"] > after]),
            "total_events": len(session.events),
            "error": session.error,
            "created_at": session.created_at,
        }

    def get(self, session_id: str, after: int = -1) -> dict:
        if type(after) is not int or after < -1:
            raise ValueError("after 必须为大于等于 -1 的整数")
        session = self._find(session_id)
        with session.lock:
            return self._state_locked(session, after)

    def command(self, session_id: str, command: str, breakpoints=None) -> dict:
        if command not in {"step", "run", "pause", "stop"}:
            raise ValueError("命令必须为 step、run、pause 或 stop")
        points = None if breakpoints is None else self._validate_breakpoints(breakpoints)
        session = self._find(session_id)
        with session.lock:
            if session.status in TERMINAL_STATUSES:
                if command == "stop":
                    return self._state_locked(session)
                raise ValueError("会话已经结束，请启动新会话")
            if command in {"step", "run"} and session.status != "paused":
                raise ValueError("只有暂停的会话可以单步或继续执行")
            if points is not None:
                session.breakpoints = points
            payload = {"command": command, "breakpoints": session.breakpoints}
            if command == "stop":
                self._transition(session, "stopped")
            elif command in {"step", "run"}:
                self._transition(session, "running")
            try:
                self._write(session, payload)
            except (OSError, ValueError) as exc:
                self._transition(session, "error", f"调试进程通信已断开：{exc}")
        if command == "stop":
            self._terminate(session, "stopped")
        return self.get(session_id)

    @staticmethod
    def _write(session: _Session, payload: dict) -> None:
        with session.write_lock:
            if session.process.stdin is None or session.process.stdin.closed:
                raise ValueError("调试输入管道已关闭")
            session.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            session.process.stdin.flush()

    @staticmethod
    def _transition(session: _Session, status: str, error: str | None = None) -> None:
        # 调用方持有 session.lock；终态不可被迟到的运行/暂停消息覆盖。
        if session.status in TERMINAL_STATUSES:
            return
        now = time.monotonic()
        if session.active_started is not None:
            session.active_seconds += now - session.active_started
        session.active_started = now if status in {"starting", "running"} else None
        session.status = status
        if error is not None:
            session.error = error

    def _read_events(self, session: _Session) -> None:
        try:
            for line in session.process.stdout:
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    with session.lock:
                        session.diagnostic = (session.diagnostic + line)[-4000:]
                    continue
                close_input = message.get("kind") == "status" and message.get("status") in TERMINAL_STATUSES
                with session.lock:
                    if session.status in TERMINAL_STATUSES:
                        if close_input:
                            self._close_input(session)
                        continue
                    if message.get("kind") == "event":
                        event = message["event"]
                        if len(session.events) < self._limits["max_events"]:
                            # 索引由接收顺序重新确认，客户端不会收到重复的序号。
                            event["index"] = len(session.events)
                            session.events.append(event)
                    elif message.get("kind") == "status" and message.get("status") in ALL_STATUSES:
                        self._transition(session, message["status"], message.get("error"))
                if close_input:
                    # 事件快照保持不可变；终态只结束控制管道，不回写历史输出。
                    self._close_input(session)
        except (OSError, ValueError) as exc:
            with session.lock:
                self._transition(session, "error", f"读取调试事件失败：{exc}")
        finally:
            returncode = session.process.wait()
            with session.lock:
                if session.status not in TERMINAL_STATUSES:
                    detail = session.diagnostic.strip()
                    self._transition(session, "error", f"调试进程意外退出（代码 {returncode}）" + (f"：{detail}" if detail else ""))
            self._close_input(session)
            for stream in (session.process.stdout,):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    @staticmethod
    def _close_input(session: _Session) -> None:
        with session.write_lock:
            try:
                if session.process.stdin is not None:
                    session.process.stdin.close()
            except OSError:
                pass

    @staticmethod
    def _read_stderr(session: _Session) -> None:
        try:
            for line in session.process.stderr:
                with session.lock:
                    session.diagnostic = (session.diagnostic + line)[-4000:]
        except (OSError, ValueError):
            pass
        finally:
            session.process.stderr.close()

    def _terminate(self, session: _Session, status: str, error: str | None = None) -> None:
        with session.lock:
            self._transition(session, status, error)
        if session.process.poll() is None:
            try:
                self._write(session, {"command": "stop"})
            except (OSError, ValueError):
                pass
            try:
                session.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                try:
                    session.process.terminate()
                except OSError:
                    pass
                try:
                    session.process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        session.process.kill()
                    except OSError:
                        pass
                    session.process.wait(timeout=2.0)

    def _watchdog(self) -> None:
        while not self._closed.wait(0.1):
            with self._lock:
                sessions = list(self._sessions.values())
            now = time.monotonic()
            expired = []
            for session in sessions:
                with session.lock:
                    if session.status in TERMINAL_STATUSES:
                        continue
                    active = session.active_seconds
                    if session.active_started is not None:
                        active += now - session.active_started
                    if active > self.timeout_seconds:
                        expired.append((session, f"有效执行时间超过 {self.timeout_seconds:g} 秒，已停止进程。"))
                    elif now - session.born > self.max_lifetime_seconds:
                        expired.append((session, "会话保留时间已到，已停止进程。"))
            for session, error in expired:
                self._terminate(session, "limited", error)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            self._terminate(session, "stopped")
        for session in sessions:
            for thread in (session.reader, session.stderr_reader):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=2.0)
        if self._watcher is not threading.current_thread():
            self._watcher.join(timeout=2.0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
