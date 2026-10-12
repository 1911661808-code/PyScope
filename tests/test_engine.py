"""真实子进程调试测试；不以 mock 代替单步、暂停与资源控制。"""

import json
from pathlib import Path
import sys
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from session_manager import SessionManager, TERMINAL_STATUSES


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.managers = []
        self.manager = self.new_manager()

    def tearDown(self):
        for manager in self.managers:
            manager.close()

    def new_manager(self, **options):
        manager = SessionManager(**options)
        self.managers.append(manager)
        return manager

    def wait_for(self, session_id, status="paused", manager=None, timeout=6):
        manager = manager or self.manager
        wanted = {status} if isinstance(status, str) else set(status)
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            latest = manager.get(session_id)
            if latest["status"] in wanted:
                return latest
            if latest["status"] in TERMINAL_STATUSES:
                self.fail(f"等待 {wanted} 时提前结束：{latest['status']} {latest['error']}")
            time.sleep(0.01)
        self.fail(f"会话等待超时：{latest}")

    def run_code(self, code, manager=None):
        manager = manager or self.manager
        session_id = manager.start(code)
        state = self.wait_for(session_id, {"paused", "error", "completed"}, manager=manager)
        if state["status"] == "paused":
            manager.command(session_id, "run")
            state = self.wait_for(session_id, TERMINAL_STATUSES, manager=manager)
        return state

    def test_initial_pause_is_before_first_executable_line(self):
        session_id = self.manager.start("# 注释\n\nx = 1\ny = x + 2\n")
        state = self.wait_for(session_id)
        self.assertEqual(state["events"][-1]["line"], 3)
        self.assertEqual(state["events"][-1]["event"], "line")
        self.assertNotIn("x", state["events"][-1]["globals"])
        self.assertEqual(state["status"], "paused")

    def test_step_and_completion_capture_real_values(self):
        session_id = self.manager.start("x = 1\ny = x + 2\nprint(y)\n")
        self.wait_for(session_id)
        self.manager.command(session_id, "step")
        state = self.wait_for(session_id)
        self.assertEqual(state["events"][-1]["line"], 2)
        self.assertEqual(state["events"][-1]["globals"]["x"]["text"], "1")
        self.assertNotIn("y", state["events"][-1]["globals"])
        self.manager.command(session_id, "run")
        state = self.wait_for(session_id, "completed")
        self.assertEqual(state["events"][-1]["output"], "3\n")

    def test_breakpoint_in_loop_and_resume(self):
        session_id = self.manager.start("total = 0\nfor i in range(3):\n    total += i\nprint(total)\n", [3])
        self.wait_for(session_id)
        for expected in range(3):
            self.manager.command(session_id, "run")
            state = self.wait_for(session_id)
            self.assertEqual(state["events"][-1]["line"], 3)
            self.assertEqual(state["events"][-1]["globals"]["i"]["text"], str(expected))
        self.manager.command(session_id, "run")
        self.assertEqual(self.wait_for(session_id, "completed")["events"][-1]["output"], "3\n")

    def test_breakpoints_can_be_replaced(self):
        session_id = self.manager.start("a = 1\nb = 2\nc = 3\n")
        self.wait_for(session_id)
        self.manager.command(session_id, "run", [3, 3])
        state = self.wait_for(session_id)
        self.assertEqual(state["breakpoints"], [3])
        self.assertEqual(state["events"][-1]["line"], 3)

    def test_recursion_exposes_call_stack_and_return_values(self):
        state = self.run_code("def fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)\nprint(fact(4))\n")
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["events"][-1]["output"], "24\n")
        deepest = max(state["events"], key=lambda event: event["depth"])
        self.assertEqual(deepest["depth"], 4)
        self.assertEqual([frame["name"] for frame in deepest["stack"]], ["<module>"] + ["fact"] * 4)
        returns = [event for event in state["events"] if event["event"] == "return" and event["function"] == "fact"]
        self.assertEqual([event["return_value"]["text"] for event in returns], ["1", "2", "6", "24"])

    def test_pause_running_and_stop_native_wait(self):
        session_id = self.manager.start("import time\ni = 0\nwhile True:\n    time.sleep(0.01)\n    i += 1\n")
        self.wait_for(session_id)
        self.manager.command(session_id, "run")
        time.sleep(0.06)
        self.manager.command(session_id, "pause")
        state = self.wait_for(session_id)
        self.assertGreater(state["total_events"], 3)
        self.assertEqual(state["events"][-1]["event"], "line")
        self.manager.command(session_id, "stop")
        self.assertEqual(self.manager.get(session_id)["status"], "stopped")
        self.assertIsNotNone(self.manager._find(session_id).process.poll())

    def test_unicode_and_cumulative_output_history(self):
        state = self.run_code("print('你好')\nprint('世界')\n")
        line_events = [event for event in state["events"] if event["event"] == "line"]
        self.assertEqual([event["output"] for event in line_events], ["", "你好\n"])
        self.assertEqual(state["events"][-1]["output"], "你好\n世界\n")

    def test_alias_cycles_and_stable_object_ids(self):
        state = self.run_code("a = []\nb = a\na.append(a)\na.append(7)\nprint(len(b))\n")
        event = state["events"][-1]
        a = event["globals"]["a"]
        self.assertEqual(a["id"], event["globals"]["b"]["id"])
        obj = next(item for item in event["objects"] if item["id"] == a["id"])
        self.assertEqual(obj["items"][0]["value"]["id"], a["id"])
        ids = {entry["globals"]["a"]["id"] for entry in state["events"] if "a" in entry["globals"]}
        self.assertEqual(ids, {a["id"]})
        early = next(entry for entry in state["events"] if "a" in entry["globals"])
        self.assertEqual(early["objects"][0]["items"], [])
        json.dumps(state, ensure_ascii=False)

    def test_containers_and_custom_objects_without_calling_repr(self):
        code = "class Node:\n    def __repr__(self):\n        raise RuntimeError('不应调用')\na = Node()\na.child = a\ndata = {'node': a, 'tuple': (1, 2), 'set': {3, 4}}\nprint('ok')\n"
        state = self.run_code(code)
        self.assertEqual(state["status"], "completed")
        event = state["events"][-1]
        self.assertTrue({"Node", "dict", "tuple", "set"}.issubset({obj["type"] for obj in event["objects"]}))

    def test_caught_and_unhandled_exception_events(self):
        caught = self.run_code("try:\n    1 / 0\nexcept ZeroDivisionError:\n    print('已处理')\n")
        self.assertEqual(caught["status"], "completed")
        self.assertTrue(any(event.get("exception", "").startswith("ZeroDivisionError") for event in caught["events"]))
        failed = self.run_code("x = 1\nraise ValueError('错误示例')\n")
        self.assertEqual(failed["status"], "error")
        self.assertIn("错误示例", failed["error"])

    def test_syntax_error_is_reported(self):
        session_id = self.manager.start("if True print('bad')\n")
        state = self.wait_for(session_id, "error")
        self.assertIn("第 1 行", state["error"])
        self.assertEqual(state["events"], [])

    def test_event_limit(self):
        manager = self.new_manager(max_events=15)
        state = self.run_code("i = 0\nwhile True:\n    i += 1\n", manager=manager)
        self.assertEqual(state["status"], "limited")
        self.assertEqual(state["total_events"], 15)
        self.assertIn("事件", state["error"])

    def test_output_limit(self):
        manager = self.new_manager(max_output=40)
        state = self.run_code("print('x' * 1000)\n", manager=manager)
        self.assertEqual(state["status"], "limited")
        self.assertIn("输出", state["error"])
        self.assertEqual(len(state["events"][-1]["output"]), 40)

    def test_stack_limit(self):
        manager = self.new_manager(max_stack_depth=8)
        state = self.run_code("def f():\n    return f()\nf()\n", manager=manager)
        self.assertEqual(state["status"], "limited")
        self.assertIn("调用栈", state["error"])

    def test_snapshot_depth_and_item_limits(self):
        manager = self.new_manager(max_depth=2, max_items=4)
        state = self.run_code("a = [[[[[1]]]]]\nb = list(range(20))\nprint('ok')\n", manager=manager)
        self.assertEqual(state["status"], "completed")
        objects = state["events"][-1]["objects"]
        texts = [item["value"].get("text", "") for obj in objects for item in obj["items"]]
        self.assertTrue(any("深度" in text for text in texts))
        self.assertTrue(any("未展开" in text for text in texts))

    def test_snapshot_object_limits_have_no_dangling_references(self):
        manager = self.new_manager(max_objects=2, max_total_objects=3)
        state = self.run_code("a = [[1], [2], [3]]\nb = {}\nprint('ok')\n", manager=manager)
        self.assertEqual(state["status"], "completed")
        final = state["events"][-1]
        object_ids = {item["id"] for item in final["objects"]}

        def check(value):
            if isinstance(value, dict):
                if value.get("kind") == "ref":
                    self.assertIn(value["id"], object_ids)
                for nested in value.values():
                    check(nested)
            elif isinstance(value, list):
                for nested in value:
                    check(nested)

        check(final)
        self.assertIn("上限", json.dumps(final, ensure_ascii=False))

    def test_exception_with_broken_string_conversion_is_reported(self):
        state = self.run_code("class Broken(Exception):\n    def __str__(self):\n        raise RuntimeError('broken str')\nraise Broken()\n")
        self.assertEqual(state["status"], "error")
        self.assertIn("Broken", state["error"])
        self.assertIn("文本表示不可用", state["error"])

    def test_watchdog_stops_blocking_native_call(self):
        manager = self.new_manager(timeout_seconds=0.4)
        state = self.run_code("import time\ntime.sleep(30)\n", manager=manager)
        self.assertEqual(state["status"], "limited")
        self.assertIn("有效执行时间", state["error"])
        deadline = time.monotonic() + 2
        while manager._find(state["id"]).process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(manager._find(state["id"]).process.poll())

    def test_pause_time_is_excluded_from_execution_budget(self):
        manager = self.new_manager(timeout_seconds=0.4)
        session_id = manager.start("x = 1\nprint(x)\n")
        self.wait_for(session_id, manager=manager)
        time.sleep(0.55)
        self.assertEqual(manager.get(session_id)["status"], "paused")
        manager.command(session_id, "run")
        self.assertEqual(self.wait_for(session_id, "completed", manager=manager)["events"][-1]["output"], "1\n")

    def test_incremental_events_and_copies(self):
        state = self.run_code("x = 2\nprint(x)\n")
        session_id = state["id"]
        incremental = self.manager.get(session_id, after=1)
        self.assertTrue(all(event["index"] > 1 for event in incremental["events"]))
        self.assertEqual(incremental["total_events"], state["total_events"])
        state["events"][0]["output"] = "篡改副本"
        self.assertEqual(self.manager.get(session_id)["events"][0]["output"], "")
        self.assertEqual(self.manager.get(session_id, after=999)["events"], [])

    def test_validation_and_parallel_session_isolation(self):
        for invalid in ("", "   ", 4):
            with self.assertRaises(ValueError):
                self.manager.start(invalid)
        for points in ([0], [True], ["1"], "1"):
            with self.assertRaises(ValueError):
                self.manager.start("x = 1", points)
        with self.assertRaises(KeyError):
            self.manager.get("missing")
        first = self.manager.start("x = 1\nprint(x)\n")
        second = self.manager.start("x = 2\nprint(x)\n")
        self.wait_for(first)
        self.wait_for(second)
        with self.assertRaises(ValueError):
            self.manager.command(first, "invalid")
        self.manager.command(first, "run")
        self.manager.command(second, "run")
        self.assertEqual(self.wait_for(first, "completed")["events"][-1]["output"], "1\n")
        self.assertEqual(self.wait_for(second, "completed")["events"][-1]["output"], "2\n")

    def test_unexpected_worker_exit_is_error(self):
        state = self.run_code("import os\nos._exit(7)\n")
        self.assertEqual(state["status"], "error")
        self.assertIn("7", state["error"])

    def test_close_terminates_paused_process_and_is_idempotent(self):
        session_id = self.manager.start("print('hello')\n")
        self.wait_for(session_id)
        process = self.manager._find(session_id).process
        self.manager.close()
        self.manager.close()
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.manager.get(session_id)["status"], "stopped")
        with self.assertRaises(ValueError):
            self.manager.start("x = 1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
