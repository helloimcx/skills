"""watchdog 看门狗单测：缺失拉起 / 幂等跳过 / /run 失败 / 拉起验证超时 / 留痕。

只测确定性逻辑：注入 fake runner/lister（线程内函数级依赖），不碰真实
schtasks / 子进程 / 网络（_ui_healthy 一律 mock 为 False，避免测试依赖
8712 真实服务）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import watchdog


class _Runner:
    """记录 /run 调用的 fake schtasks runner。"""

    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, argv):
        self.calls.append(list(argv))
        return type("R", (), {"returncode": self.rc,
                              "stdout": "", "stderr": "boom"})


class _Lister:
    """可控进程列表：首次查询返回 initial，之后返回 then（模拟拉起后出现）。"""

    def __init__(self, initial=(), then=()):
        self.initial = list(initial)
        self.then = list(then)
        self.calls = 0

    def __call__(self, script):
        self.calls += 1
        return list(self.initial) if self.calls == 1 else list(self.then)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.log = Path(self.tmp) / "watchdog.jsonl"
        patcher = mock.patch("watchdog.watchdog_log_path",
                             return_value=self.log)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 默认面板探活失败（单元测试环境无真实 8712；对 cp-server 用例会判 warning）
        ui = mock.patch("watchdog._ui_healthy", return_value=False)
        ui.start()
        self.addCleanup(ui.stop)

    # -- 幂等：进程在则跳过 ------------------------------------------------
    def test_alive_process_skips_spawn(self):
        runner = _Runner()
        entry = watchdog._check_one("code-loop-worker-agent", "worker_agent.py",
                                    runner=runner,
                                    lister=_Lister(initial=[(1, "iso")]))
        self.assertTrue(entry["alive"])
        self.assertEqual(entry["pids"], [1])
        self.assertEqual(entry["action"], "none")
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["exit"], 0)
        self.assertEqual(runner.calls, [])  # 从未 /run

    # 缺失 → /run → 新进程验证 -------------------------------------------------
    def test_missing_triggers_spawn_verifies_new_pid(self):
        runner = _Runner()
        entry = watchdog._check_one("code-loop-worker-agent", "worker_agent.py",
                                    runner=runner,
                                    lister=_Lister(initial=[], then=[(42, "iso")]))
        self.assertFalse(entry["alive"])
        self.assertEqual(entry["action"], "spawn")
        self.assertEqual(entry["result"], "ok (new pid 42)")
        self.assertEqual(entry["exit"], 0)
        self.assertEqual(runner.calls, [["/run", "/tn", "code-loop-worker-agent"]])

    # /run 失败 ------------------------------------------------------------
    def test_spawn_run_failure_reported_fail(self):
        runner = _Runner(rc=1)
        entry = watchdog._check_one("code-loop-cp-serve", "cp_server.py",
                                    runner=runner, lister=_Lister())
        self.assertEqual(entry["action"], "spawn")
        self.assertTrue(entry["result"].startswith("fail (/run rc=1"))
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["exit"], 1)

    # /run 后未见新进程 -> 失败 -------------------------------------------
    def test_spawn_no_new_process_within_window_fails(self):
        entry = watchdog._check_one("code-loop-cp-serve", "cp_server.py",
                                    runner=_Runner(), lister=_Lister(),
                                    sleeper=lambda _s: None, wait_seconds=0)
        self.assertEqual(
            entry["result"],
            "fail (/run 后未见新进程——启动器可能未注册或崩溃)")
        self.assertFalse(entry["ok"])

    # cp-server 进程在但面板失联 -> warning 且整体 ok=False -----------------
    def test_cp_alive_but_ui_dead_marks_failed_with_warning(self):
        entry = watchdog._check_one("code-loop-cp-serve", "cp_server.py",
                                    runner=_Runner(),
                                    lister=_Lister(initial=[(7, "iso")]))
        self.assertTrue(entry["alive"])
        self.assertEqual(entry["action"], "none")
        self.assertFalse(entry["ok"])       # ui 探活失败使整体非健康
        self.assertIn("8712", entry["warning"])

    # 留痕必须逐条写入 ------------------------------------------------------
    def test_journal_always_written(self):
        watchdog._check_one("code-loop-worker-agent", "worker_agent.py",
                            runner=_Runner(),
                            lister=_Lister(initial=[], then=[(9, "iso")]))
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["task"], "code-loop-worker-agent")

    # main(--check) 只读：缺进程也不拉起 ------------------------------------
    def test_check_mode_is_read_only(self):
        # 真实路径：--check 下进程缺失时绝不发射 schtasks /run
        with mock.patch.object(watchdog, "RESIDENT_TASKS",
                               {"code-loop-worker-agent": "worker_agent.py"}):
            runner = _Runner()
            with mock.patch("watchdog._ps_processes", return_value=[]), \
                 mock.patch("watchdog._schtasks", runner):
                rc = watchdog.main(["--check"])
        self.assertEqual(rc, 0)  # 只读模式不因巡检失败退出
        self.assertEqual(runner.calls, [])  # --check 下从未 /run

    # --check 缺失进程报告为只读态而非 spawn --------------------------------
    def test_check_one_check_only_reports_missing(self):
        entry = watchdog._check_one("code-loop-worker-agent", "worker_agent.py",
                                    runner=_Runner(),
                                    lister=_Lister(initial=[], then=[(9, "iso")]),
                                    check_only=True)
        self.assertFalse(entry["alive"])
        self.assertEqual(entry["action"], "report")
        self.assertIn("只读", entry["result"])
        self.assertFalse(entry["ok"])


if __name__ == "__main__":
    unittest.main()