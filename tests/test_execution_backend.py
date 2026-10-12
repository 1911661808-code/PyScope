"""无需 Docker 的策略测试，以及真实本地子进程的协议攻击/超时测试。"""
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import execution_backend as backend
from session_manager import SessionManager, TERMINAL_STATUSES

IMAGE_ID = "sha256:" + "a" * 64


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeProcess:
    def __init__(self, output="{}", wait_error=False):
        self.stdin, self.stdout, self.stderr = io.StringIO(), io.StringIO(output), io.StringIO()
        self.returncode = None
        self.wait_error = wait_error

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.wait_error and self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = self.returncode or 0
        return self.returncode

    def kill(self):
        self.returncode = -9

    terminate = kill


class DockerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        def docker(instance, arguments, **kwargs):
            self.commands.append(arguments)
            if arguments[:2] == ["image", "inspect"]:
                return completed(IMAGE_ID + " linux")
            if arguments[0] == "inspect":
                return completed("true")
            if arguments[0] == "info":
                return completed('["name=rootless", "name=seccomp,profile=builtin"]')
            if arguments[0] == "ps":
                return completed("")
            return completed("container")
        self.patch = patch.object(backend.DockerBackend, "_docker", docker)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.instance = backend.DockerBackend(runtime="runsc")
        self.addCleanup(self.instance.close)

    def test_pins_inspected_image_and_enforces_limits_for_each_worker(self):
        for worker in ("trace_worker", "profile_worker", "analysis_worker", "probe"):
            command = self.instance._create_command("pyscope-example", worker)
            for token in ("--pull=never", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
                self.assertIn(token, command)
            values = {"--network": "none", "--user": "65532:65532", "--pids-limit": "32", "--memory": "128m",
                      "--memory-swap": "128m", "--cpus": "0.5", "--runtime": "runsc", "--workdir": "/work", "--ipc": "none"}
            for flag, expected in values.items():
                self.assertEqual(command[command.index(flag) + 1], expected)
            self.assertEqual(command.count("--tmpfs"), 2)
            self.assertIn("/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777", command)
            self.assertIn(IMAGE_ID, command)
            for forbidden in ("--privileged", "--volume", "-v", "--mount", "--env", "-e", "--env-file", "--pid=host"):
                self.assertNotIn(forbidden, command)
            self.assertNotIn(self.instance.image, command)

    def test_normal_wait_and_stop_explicitly_remove_container(self):
        for stopping in (False, True):
            process = FakeProcess()
            with patch.object(backend, "_popen", return_value=process):
                wrapped = self.instance._spawn("trace_worker")
                name = wrapped.name
                if stopping:
                    wrapped.terminate()
                else:
                    wrapped.wait(timeout=1)
                wrapped.kill()
            self.assertEqual(sum(cmd == ["rm", "--force", name] for cmd in self.commands), 1)
            self.assertNotIn(name, self.instance._containers)

    def test_create_error_always_attempts_remove(self):
        original = self.instance._docker
        def failure(arguments, **kwargs):
            if arguments[0] == "create":
                raise backend.BackendError("create timeout")
            return original(arguments, **kwargs)
        with patch.object(self.instance, "_docker", side_effect=failure):
            with self.assertRaises(backend.BackendError):
                self.instance._spawn("trace_worker")
        self.assertTrue(any(cmd[:2] == ["rm", "--force"] for cmd in self.commands))
        self.assertFalse(self.instance._containers)

    def test_start_error_always_removes_and_closes_pipes(self):
        process = FakeProcess()
        process.returncode = 1
        original = self.instance._docker
        def stopped(arguments, **kwargs):
            if arguments[0] == "inspect":
                return completed("false")
            return original(arguments, **kwargs)
        with patch.object(self.instance, "_docker", side_effect=stopped), patch.object(backend, "_popen", return_value=process):
            with self.assertRaises(backend.BackendError):
                self.instance._spawn("trace_worker")
        self.assertTrue(process.stdin.closed)
        self.assertFalse(self.instance._containers)

    def test_one_shot_timeout_removes_container(self):
        process = FakeProcess(wait_error=True)
        with patch.object(backend, "_popen", return_value=process):
            wrapped = self.instance._spawn("profile_worker")
            with self.assertRaises(backend.ExecutionTimeout):
                backend._collect_result(wrapped, "while True: pass", .1)
        self.assertIn(["rm", "--force", wrapped.name], self.commands)

    def test_one_shot_protocol_error_always_removes_container(self):
        with patch.object(backend, "_popen", return_value=FakeProcess("[]")):
            wrapped = self.instance._spawn("profile_worker")
            with self.assertRaises(backend.ProtocolError):
                backend._collect_result(wrapped, "pass", 1)
        self.assertIn(["rm", "--force", wrapped.name], self.commands)

    def test_unavailable_or_non_linux_image_never_falls_back_or_pulls(self):
        for response in (completed("", returncode=1), completed(IMAGE_ID + " windows"), completed("fake linux")):
            with patch.object(backend.DockerBackend, "_docker", return_value=response), patch.object(backend, "_popen") as popen:
                with self.assertRaises(backend.BackendError):
                    backend.DockerBackend()
                popen.assert_not_called()

    def test_verify_checks_effective_cgroups_and_privileges(self):
        valid = {"uid": 65532, "gid": 65532, "root_readonly": True, "networks": ["lo"],
                 "no_new_privs": "1", "cap_effective": "0000000000000000", "memory": str(128 * 1024 * 1024),
                 "swap": "0", "pids": "32", "cpu": "50000 100000"}
        for field, value in (("memory", "max"), ("cpu", "max 100000"), ("pids", "max"), ("uid", 0),
                             ("root_readonly", False), ("networks", ["eth0", "lo"]), ("no_new_privs", "0"),
                             ("cap_effective", "1"), ("swap", "max")):
            with self.subTest(field=field), patch.object(self.instance, "_spawn"), patch.object(backend, "_collect_result", return_value={**valid, field: value}):
                with self.assertRaises(backend.BackendError):
                    self.instance.verify()
                self.assertFalse(self.instance._verified)
        with patch.object(self.instance, "_spawn"), patch.object(backend, "_collect_result", return_value=valid):
            self.assertTrue(self.instance.verify()["ok"])

    def test_no_workload_can_bypass_failed_verification(self):
        with patch.object(self.instance, "verify", side_effect=backend.BackendError("limits ineffective")), patch.object(self.instance, "_spawn") as spawn:
            for call in (self.instance.spawn_trace, lambda: self.instance.analyze("x=1"), lambda: self.instance.run_profile("x=1")):
                with self.assertRaises(backend.BackendError):
                    call()
            spawn.assert_not_called()

    def test_rootless_is_mandatory_and_orphan_cleanup_is_label_scoped(self):
        with patch.object(self.instance, "_docker", return_value=completed('["name=seccomp,profile=builtin"]')):
            with self.assertRaises(backend.BackendError):
                self.instance.verify()
        self.instance._rootless_verified = True
        orphan = "b" * 64
        original = self.instance._docker
        def orphan_docker(arguments, **kwargs):
            if arguments[0] == "ps":
                self.commands.append(arguments)
                return completed(orphan)
            return original(arguments, **kwargs)
        with patch.object(self.instance, "_docker", side_effect=orphan_docker):
            self.assertEqual(self.instance.cleanup_orphans(), 1)
        self.assertIn(["ps", "--all", "--quiet", "--no-trunc", "--filter", "label=pyscope.worker=true"], self.commands)
        self.assertIn(["rm", "--force", orphan], self.commands)


class ProtocolTests(unittest.TestCase):
    def test_unterminated_line_and_cumulative_output_are_bounded(self):
        for stream, line_limit, total in ((io.StringIO("x" * 11), 10, 100), (io.StringIO("aa\n" * 10), 10, 20)):
            with self.assertRaises(backend.OutputLimitError):
                list(backend.bounded_lines(stream, line_limit=line_limit, total_limit=total))

    def test_invalid_json_shapes_and_nonfinite_numbers_are_rejected(self):
        for text in ("[]", "null", '"text"', '{"a":NaN}', "[" * 2000 + "]" * 2000):
            with self.assertRaises(backend.ProtocolError):
                backend.parse_json(text)
        for value in (True, 10 ** 1000, float("inf"), -1):
            with self.assertRaises(backend.ProtocolError):
                backend._number(value)
        with self.assertRaises(backend.ProtocolError):
            backend._text("\ud800")

    def test_message_and_event_types_rejected_without_host_exception(self):
        with SessionManager() as manager:
            for value in ({"kind": "status", "status": []}, {"kind": "event", "event": []},
                          {"kind": "event", "event": {"event": {}}}, {"kind": "bogus"}):
                with self.assertRaises(backend.ProtocolError):
                    backend.validate_trace_message(value, manager._limits)

    def test_frozen_local_worker_dispatch_preserved(self):
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(backend.LocalBackend._command("trace_worker"), [sys.executable, "--trace-worker"])
            self.assertEqual(backend.LocalBackend._command("profile_worker"), [sys.executable, "--profile-worker"])


class RealLocalBoundaryTests(unittest.TestCase):
    def test_session_trace_budget_is_configurable_and_enforced(self):
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                SessionManager(max_trace_chars=invalid)
        with SessionManager(max_trace_chars=700) as manager:
            sid = manager.start("value = 'x' * 1000\nprint(value)\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = manager.get(sid)
                if state["status"] == "paused":
                    manager.command(sid, "run")
                if state["status"] in TERMINAL_STATUSES:
                    break
                time.sleep(.01)
            self.assertEqual(state["status"], "limited")
            self.assertIn("通信限制", state["error"])

    def run_trace(self, code):
        with SessionManager(timeout_seconds=2) as manager:
            sid = manager.start(code)
            deadline = time.monotonic() + 5
            continued = False
            while time.monotonic() < deadline:
                state = manager.get(sid)
                if state["status"] == "paused" and not continued:
                    manager.command(sid, "run")
                    continued = True
                if state["status"] in TERMINAL_STATUSES:
                    return state
                time.sleep(.01)
            self.fail("trace timeout")

    def test_raw_stdout_cannot_crash_host_reader(self):
        state = self.run_trace("import os\nos.write(1, b'[]\\n')\n")
        self.assertEqual(state["status"], "error")
        self.assertIn("对象", state["error"])

    def test_raw_stdout_without_newline_is_limited(self):
        state = self.run_trace("import os\nos.write(1, b'x' * 2100000)\n")
        self.assertEqual(state["status"], "limited")

    def test_raw_stderr_without_newline_is_limited(self):
        state = self.run_trace("import os\nos.write(2, b'x' * 100000)\n")
        self.assertEqual(state["status"], "limited")

    def test_local_profile_and_analysis_still_work(self):
        instance = backend.LocalBackend()
        result = instance.run_profile("def add(a, b):\n    return a+b\nprint(add(2,3))\n")
        self.assertEqual(result["output"], "5\n")
        self.assertEqual(result["functions"][0]["calls"], 1)
        self.assertEqual(instance.analyze("x=1")["counts"]["lines"], 1)

    def test_profile_timeout_and_raw_output_are_bounded(self):
        instance = backend.LocalBackend(timeout_seconds=.15)
        with self.assertRaises(backend.ExecutionTimeout):
            instance.run_profile("import time\ntime.sleep(10)")
        with self.assertRaises(backend.ProtocolError):
            backend.LocalBackend().run_profile("import os\nos.write(1,b'x'*2100000)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
