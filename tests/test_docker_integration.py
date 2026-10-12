"""必须有真实 Linux Docker 引擎与已构建 worker 镜像；普通测试明确跳过。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution_backend import DockerBackend, ExecutionTimeout
from session_manager import SessionManager, TERMINAL_STATUSES


@unittest.skipUnless(os.environ.get("PYSCOPE_DOCKER_TESTS") == "1", "需要显式启用真实 Docker 集成测试")
class DockerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.backend = DockerBackend(image=os.environ.get("PYSCOPE_WORKER_IMAGE", "pyscope-worker:1.1.0"),
                                     runtime=os.environ.get("PYSCOPE_DOCKER_RUNTIME") or None)
        self.backend.verify(require_rootless=os.environ.get("PYSCOPE_REQUIRE_ROOTLESS") == "1")
        self.addCleanup(self.backend.close)

    def no_containers(self):
        self.assertFalse(self.backend._containers, "本次后端仍有未清理容器")
        listed = self.backend._docker(["ps", "--all", "--quiet", "--filter", "label=pyscope.worker=true"])
        self.assertEqual(listed.returncode, 0)
        self.assertFalse(listed.stdout.strip(), "Docker 中仍有本应用标签的容器")

    def wait_state(self, manager, sid, wanted):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = manager.get(sid)
            if state["status"] in wanted:
                return state
            time.sleep(.02)
        self.fail("Docker trace state timeout")

    def test_real_debug_step_breakpoint_and_cleanup(self):
        with SessionManager(backend=self.backend) as manager:
            sid = manager.start("a=1\nb=2\nprint(a+b)\n", [3])
            self.assertEqual(self.wait_state(manager, sid, {"paused"})["events"][-1]["line"], 1)
            manager.command(sid, "step")
            self.assertEqual(self.wait_state(manager, sid, {"paused"})["events"][-1]["globals"]["a"]["text"], "1")
            manager.command(sid, "run")
            self.assertEqual(self.wait_state(manager, sid, {"paused"})["events"][-1]["line"], 3)
            manager.command(sid, "run")
            state = self.wait_state(manager, sid, TERMINAL_STATUSES)
            self.assertEqual(state["status"], "completed", state["error"])
            self.assertEqual(state["events"][-1]["output"], "3\n")
        self.no_containers()

    def test_real_analysis_and_profile(self):
        self.assertEqual(self.backend.analyze("def f():\n    return 2\nf()\n")["counts"]["functions"], 1)
        with self.assertRaises(SyntaxError):
            self.backend.analyze("if x print(x)")
        result = self.backend.run_profile("def f():\n    return 2\nprint(f())\n")
        self.assertEqual(result["output"], "2\n")
        self.assertEqual(result["functions"][0]["calls"], 1)
        self.no_containers()

    def test_real_network_host_files_environment_and_rootfs_isolation(self):
        os.environ["PYSCOPE_ISOLATION_TEST_SECRET"] = "host-only-sentinel"
        self.addCleanup(os.environ.pop, "PYSCOPE_ISOLATION_TEST_SECRET", None)
        code = '''import json, os, pathlib, socket
result = {'uid': os.getuid(), 'secret_absent': 'PYSCOPE_ISOLATION_TEST_SECRET' not in os.environ,
 'host_absent': not pathlib.Path('/var/run/docker.sock').exists(),
 'networks': sorted(os.listdir('/sys/class/net'))}
try:
    pathlib.Path('/opt/pyscope/write-test').write_text('test')
    result['readonly'] = False
except OSError:
    result['readonly'] = True
try:
    socket.create_connection(('1.1.1.1', 53), timeout=.2)
    result['network_blocked'] = False
except OSError:
    result['network_blocked'] = True
pathlib.Path('/work/test-file').write_text('temporary')
result['temporary_writable'] = pathlib.Path('/work/test-file').read_text() == 'temporary'
print(json.dumps(result))
'''
        result = json.loads(self.backend.run_profile(code)["output"])
        self.assertEqual(result["uid"], 65532)
        self.assertEqual(result["networks"], ["lo"])
        for key in ("secret_absent", "host_absent", "readonly", "network_blocked", "temporary_writable"):
            self.assertTrue(result[key], key)
        # 每次运行文件系统独立，上次创建的临时文件不能留下。
        self.assertEqual(self.backend.run_profile("from pathlib import Path\nprint(Path('/work/test-file').exists())")["output"], "False\n")
        self.no_containers()

    def test_real_stop_cleans_blocked_container(self):
        with SessionManager(backend=self.backend) as manager:
            sid = manager.start("import time\ntime.sleep(60)\n")
            self.wait_state(manager, sid, {"paused"})
            manager.command(sid, "run")
            time.sleep(.2)
            manager.command(sid, "stop")
            self.assertEqual(manager.get(sid)["status"], "stopped")
        self.no_containers()

    def test_real_timeout_cleans_container(self):
        self.backend.timeout_seconds = .2
        with self.assertRaises(ExecutionTimeout):
            self.backend.run_profile("import time\ntime.sleep(60)")
        self.no_containers()

    def test_real_malformed_protocol_cleans_container(self):
        with SessionManager(backend=self.backend) as manager:
            sid = manager.start("import os\nos.write(1,b'[]\\n')\n")
            self.wait_state(manager, sid, {"paused"})
            manager.command(sid, "run")
            self.assertEqual(self.wait_state(manager, sid, TERMINAL_STATUSES)["status"], "error")
        self.no_containers()


if __name__ == "__main__":
    unittest.main(verbosity=2)
