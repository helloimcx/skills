#!/usr/bin/env python3
"""update_deploy.py 的分层单测（部署副本确定性更新器）。

所有 git 行为跑在 tmp 目录内的真实本地仓库上（bare origin + seed + deploy
clone，零网络）；schtasks 层用注入的 fake runner 断言调用序列，单测不碰真实
计划任务。journal / lock 路径通过 monkeypatch 模块函数指到 tmp。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
for _p in (str(SERVICE), str(SERVICE / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import update_deploy as upd


def _git(root, *args):
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"git {args} rc={proc.returncode}: {proc.stderr}")
    return proc.stdout.strip()


class GitFixture:
    """bare origin + seed（推进 origin 用）+ deploy（被更新的部署副本）。

    deploy 复刻真实仓库布局：services/code-loop/{codeloop,scripts/state_space.py}
    （exit 0），让激活门三件在 scratch 上真实可跑；import 冒烟的模块清单由
    各测试 monkeypatch 成 ('codeloop',)。
    """

    SERVICE = Path("services") / "code-loop"

    def __init__(self, base: Path):
        self.origin = base / "origin.git"
        _git(base, "init", "--bare", str(self.origin), "-b", "main")
        self.seed = base / "seed"
        _git(base, "clone", str(self.origin), str(self.seed))
        _git(self.seed, "config", "user.email", "t@t")
        _git(self.seed, "config", "user.name", "t")
        (self.seed / "README.md").write_text("v1", encoding="utf-8")
        svc = self.seed / self.SERVICE
        (svc / "codeloop").mkdir(parents=True)
        (svc / "codeloop" / "__init__.py").write_text("", encoding="utf-8")
        (svc / "scripts").mkdir()
        (svc / "scripts" / "state_space.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", "init service tree")
        _git(self.seed, "push", "origin", "main")
        self.deploy = base / "deploy"
        _git(base, "clone", str(self.origin), str(self.deploy))
        _git(self.deploy, "config", "user.email", "t@t")
        _git(self.deploy, "config", "user.name", "t")

    def service(self, clone: Path = None) -> Path:
        return (clone or self.deploy) / self.SERVICE

    def advance_origin(self, filename="docs/note.md", text="n1"):
        """在 seed 上新增一个与 README 无关的文件并推送——产生 behind=N。"""
        path = self.seed / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", f"advance {filename}")
        _git(self.seed, "push", "origin", "main")


class UpdateDeployTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.fx = GitFixture(base)
        self.home = base / "home"
        self.journal = self.home / "logs" / "update-deploy.jsonl"
        self.lockfile = self.home / "locks" / "update-deploy.lock"

        def _journal_path():
            return self.journal

        def _lock_path():
            return self.lockfile

        upd.journal_path = _journal_path
        upd.lock_path = _lock_path
        upd.IMPORT_SMOKE_MODULES = ("codeloop",)

    def journal_lines(self):
        if not self.journal.is_file():
            return []
        return [json.loads(line) for line in
                self.journal.read_text(encoding="utf-8").splitlines()]


class TestResolveRoot(UpdateDeployTestBase):
    def test_git_dir_ok(self):
        self.assertEqual(upd.resolve_root(str(self.fx.deploy)),
                         Path(self.fx.deploy).resolve())

    def test_git_file_is_linked_worktree_refused(self):
        fake = Path(self._tmp.name) / "wt"
        fake.mkdir()
        (fake / ".git").write_text("gitdir: somewhere", encoding="utf-8")
        with self.assertRaises(upd.DeployError):
            upd.resolve_root(str(fake))

    def test_no_git_refused(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        with self.assertRaises(upd.DeployError):
            upd.resolve_root(str(plain))

    def test_missing_root_refused(self):
        with self.assertRaises(upd.DeployError):
            upd.resolve_root(str(Path(self._tmp.name) / "nope"))

    def test_repo_without_service_layout_refused(self):
        # 真实仓库根含 services/code-loop/codeloop；缺此布局的目录不是部署检出。
        repo = Path(self._tmp.name) / "other"
        _git(repo.parent, "init", str(repo), "-b", "main")
        with self.assertRaises(upd.DeployError) as ctx:
            upd.resolve_root(str(repo))
        self.assertIn("services", str(ctx.exception))

    def test_default_root_is_repo_root_not_service_dir(self):
        """QA 发现的缺陷回归锁：默认根必须是**仓库根**（git 操作跨整个工作树），
        而不是脚本所在的 service 子目录——后者没有 .git，真实部署下默认调用
        会直接失败。"""
        svc_scripts = self.fx.service() / "scripts"
        real = upd._script_dir
        upd._script_dir = lambda: svc_scripts
        try:
            self.assertEqual(upd.resolve_root(), Path(self.fx.deploy).resolve())
        finally:
            upd._script_dir = real

    def test_default_root_refuses_real_linked_worktree(self):
        """默认根落在 linked worktree（.git 是文件）时必须拒绝——用真实
        git worktree 构造，而不是手写 .git 文件。"""
        wt = Path(self._tmp.name) / "linked"
        _git(self.fx.deploy, "worktree", "add", str(wt), "-b", "wt-branch")
        real = upd._script_dir
        upd._script_dir = lambda: wt / self.fx.SERVICE / "scripts"
        try:
            with self.assertRaises(upd.DeployError) as ctx:
                upd.resolve_root()
        finally:
            upd._script_dir = real
        self.assertIn("linked worktree", str(ctx.exception))


class TestSurvey(UpdateDeployTestBase):
    def test_up_to_date(self):
        state = upd.survey(Path(self.fx.deploy))
        self.assertEqual((state["behind"], state["ahead"]), (0, 0))
        self.assertEqual(upd.classify(state, force=False)[0], "up-to-date")

    def test_behind_and_update(self):
        self.fx.advance_origin()
        state = upd.survey(Path(self.fx.deploy))
        self.assertEqual(state["behind"], 1)
        self.assertEqual(upd.classify(state, force=False)[0], "update")

    def test_ahead_refused_even_forced(self):
        (self.fx.deploy / "local.txt").write_text("local", encoding="utf-8")
        _git(self.fx.deploy, "add", "-A")
        _git(self.fx.deploy, "commit", "-m", "local commit")
        state = upd.survey(Path(self.fx.deploy))
        self.assertEqual(state["ahead"], 1)
        self.assertEqual(upd.classify(state, force=True)[0], "refuse")

    def test_diverged_refused_even_forced(self):
        self.fx.advance_origin()
        (self.fx.deploy / "local.txt").write_text("local", encoding="utf-8")
        _git(self.fx.deploy, "add", "-A")
        _git(self.fx.deploy, "commit", "-m", "local commit")
        state = upd.survey(Path(self.fx.deploy))
        self.assertEqual((state["behind"], state["ahead"]), (1, 1))
        self.assertEqual(upd.classify(state, force=True)[0], "refuse")

    def test_dirty_tracked_blocks_update_untracked_ignored(self):
        readme = self.fx.deploy / "README.md"
        readme.write_text("dirty", encoding="utf-8")
        (self.fx.deploy / "untracked.log").write_text("x", encoding="utf-8")
        self.fx.advance_origin()
        state = upd.survey(Path(self.fx.deploy))
        self.assertEqual(len(state["dirty"]), 1)
        self.assertIn("README.md", state["dirty"][0])
        self.assertEqual(upd.classify(state, force=False)[0], "refuse")

    def test_force_allows_dirty_disjoint_update(self):
        readme = self.fx.deploy / "README.md"
        readme.write_text("dirty", encoding="utf-8")
        self.fx.advance_origin()  # 推进只碰 docs/note.md，不与 dirty 冲突
        state = upd.survey(Path(self.fx.deploy))
        self.assertEqual(upd.classify(state, force=True)[0], "update")

    def test_fetch_failure_clean_error(self):
        import shutil
        import stat

        def _force_remove(func, path, _exc):
            os.chmod(path, stat.S_IWRITE)  # git 只读对象文件在 Windows 上需先解只读
            func(path)

        shutil.rmtree(self.fx.origin, onerror=_force_remove)
        with self.assertRaises(upd.DeployError):
            upd.survey(Path(self.fx.deploy))
        # 部署副本工作区完好
        self.assertEqual(
            (self.fx.deploy / "README.md").read_text(encoding="utf-8"), "v1")


class TestLock(UpdateDeployTestBase):
    def test_acquire_release(self):
        lock = upd.UpdateLock(self.lockfile)
        lock.acquire()
        self.assertTrue(self.lockfile.is_file())
        self.assertEqual(self.lockfile.read_text(encoding="utf-8"),
                         str(upd.os.getpid()))
        lock.release()
        self.assertFalse(self.lockfile.is_file())

    def test_second_instance_blocked(self):
        lock = upd.UpdateLock(self.lockfile)
        lock.acquire()
        with self.assertRaises(upd.DeployError):
            upd.UpdateLock(self.lockfile).acquire()
        lock.release()

    def test_stale_lock_taken_over(self):
        # 真实死 pid 断言是环境敏感的（并行门禁下 pid 可能被复用，见 issue#216
        # 教训），接管机制用确定性桩测：锁存在且判活为死 → 必须接管成功。
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile.write_text("123456", encoding="utf-8")
        real = upd._pid_alive
        upd._pid_alive = lambda pid: False
        try:
            lock = upd.UpdateLock(self.lockfile)
            lock.acquire()
            lock.release()
        finally:
            upd._pid_alive = real

    def test_pid_alive_probe_basics(self):
        self.assertTrue(upd._pid_alive(upd.os.getpid()))
        self.assertFalse(upd._pid_alive(0))
        self.assertFalse(upd._pid_alive(-1))

    def test_fresh_pidless_lock_not_taken_over(self):
        # 并发实例刚 O_EXCL 创建、pid 尚未落盘的瞬间不得被接管（否则双写，
        # 对齐 collect.py issue #205 的 mid-write 语义）。
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile.write_text("", encoding="utf-8")
        with self.assertRaises(upd.DeployError):
            upd.UpdateLock(self.lockfile).acquire()

    def test_stale_pidless_lock_taken_over(self):
        import time as _time
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        self.lockfile.write_text("garbage", encoding="utf-8")
        old = _time.time() - upd.PIDLESS_GRACE_SECONDS - 5
        os.utime(self.lockfile, (old, old))
        lock = upd.UpdateLock(self.lockfile)
        lock.acquire()
        lock.release()

    def test_release_does_not_delete_taken_over_lock(self):
        # 被接管过的运行收尾时不得删除新 holder 的锁（对齐 collect.py
        # _release_lock 的归属校验）。
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        stale = upd.UpdateLock(self.lockfile)
        stale.acquire()
        self.lockfile.write_text("999999", encoding="utf-8")  # 模拟被接管
        stale.release()
        self.assertTrue(self.lockfile.is_file())


class TestFastForward(UpdateDeployTestBase):
    def test_ff_advances_to_origin_main(self):
        self.fx.advance_origin()
        before = _git(self.fx.deploy, "rev-parse", "HEAD")
        origin_head = _git(self.fx.seed, "rev-parse", "origin/main")
        frm, to = upd.fast_forward(Path(self.fx.deploy))
        self.assertNotEqual(frm, to)
        self.assertEqual(frm, before)
        self.assertEqual(to, origin_head)

    def test_diverged_ff_refused_and_head_unchanged(self):
        self.fx.advance_origin()
        (self.fx.deploy / "local.txt").write_text("local", encoding="utf-8")
        _git(self.fx.deploy, "add", "-A")
        _git(self.fx.deploy, "commit", "-m", "local commit")
        before = _git(self.fx.deploy, "rev-parse", "HEAD")
        with self.assertRaises(upd.DeployError):
            upd.fast_forward(Path(self.fx.deploy))
        self.assertEqual(_git(self.fx.deploy, "rev-parse", "HEAD"), before)


class TestActivationGate(UpdateDeployTestBase):
    def test_healthy_passes(self):
        self.assertEqual(
            upd.run_activation_gate(Path(self.fx.deploy),
                                    modules=("codeloop",)), [])

    def test_syntax_error_reported(self):
        (self.fx.service() / "codeloop" / "bad.py").write_text(
            "def broken(:\n", encoding="utf-8")
        failures = upd.run_activation_gate(Path(self.fx.deploy),
                                           modules=("codeloop",))
        self.assertTrue(failures)
        self.assertTrue(any("bad.py" in f for f in failures))

    def test_missing_state_space_reported(self):
        (self.fx.service() / "scripts" / "state_space.py").unlink()
        failures = upd.run_activation_gate(Path(self.fx.deploy),
                                           modules=("codeloop",))
        self.assertTrue(any("state_space" in f for f in failures))

    def test_import_smoke_failure_reported(self):
        (self.fx.service() / "codeloop" / "boom.py").write_text(
            "raise RuntimeError('no module named this')\n", encoding="utf-8")
        (self.fx.service() / "codeloop" / "__init__.py").write_text(
            "import codeloop.boom\n", encoding="utf-8")
        failures = upd.run_activation_gate(Path(self.fx.deploy),
                                           modules=("codeloop",))
        self.assertTrue(any("import" in f.lower() for f in failures))


class TestConsoleDecoding(UpdateDeployTestBase):
    """QA 实测发现：schtasks 输出是系统 ANSI/OEM 代码页（中文 Windows=cp936），
    按 UTF-8 解会乱码甚至失败；状态串要落台账必须有可读值。"""

    def test_gbk_bytes_decoded_on_windows(self):
        if os.name != "nt":
            self.skipTest("mbcs 仅 Windows")
        raw = '"\\code-loop-worker-agent","N/A","就绪"'.encode("mbcs")
        self.assertIn("就绪", upd._decode_console(raw))

    def test_windows_prefers_console_codepage(self):
        """Windows 上 mbcs 优先——schtasks 输出永远是 ANSI/OEM 码页，而 cp936
        几乎能无错解码任意字节对（utf-8 输入会被解成乱码但不报错），所以优先序
        必须钉在 mbcs，不能靠"解码是否抛错"来选。"""
        if os.name != "nt":
            self.skipTest("mbcs 仅 Windows")
        self.assertIn("就绪", upd._decode_console("就绪".encode("mbcs")))

    def test_ascii_stable_across_encodings(self):
        raw = b'"\\code-loop-cp-serve","N/A","Ready"'
        self.assertEqual(upd._decode_console(raw), raw.decode("ascii"))

    def test_garbage_never_raises(self):
        self.assertIsInstance(upd._decode_console(b"\xff\xfe\x00bad"), str)

    def test_gate_pins_child_encoding(self):
        """激活门必须把子进程输出钉成 UTF-8——宿主 locale/PYTHONUTF8 不可依赖
        （实测本机 locale 报 utf-8 而 schtasks 实际发 cp936）。"""
        calls = []
        real = upd.subprocess.run

        def fake_run(argv, **kw):
            calls.append(kw)
            return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

        upd.subprocess.run = fake_run
        try:
            failures = upd.run_activation_gate(Path(self.fx.deploy),
                                               modules=("codeloop",))
        finally:
            upd.subprocess.run = real
        self.assertEqual(failures, [])
        self.assertEqual(len(calls), 2)  # import 冒烟 + state_space check
        for kw in calls:
            self.assertEqual(kw["env"]["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(kw["encoding"], "utf-8")


class TestRestartResident(UpdateDeployTestBase):
    """2026-09-23 生产实测：常驻任务的启动器是 Run(..., 0, False) 分离启动，
    任务实例不持有 python 进程 → /end 杀不掉、任务状态与 /run 退出码都证明不了
    重启生效（实测报 ok 而进程原封不动）。判据必须是进程身份。"""

    def setUp(self):
        super().setUp()
        self.upd_restart = upd.restart_resident
        self._orig_os_name = upd.os.name
        upd.os.name = 'nt'
        # 轮询超时压到 0：单次探测即定论，避免测试真等待
        self._saved = (upd.RESTART_SETTLE_SECONDS, upd.RESTART_SPAWN_SECONDS)
        upd.RESTART_SETTLE_SECONDS = 0
        upd.RESTART_SPAWN_SECONDS = 0

    def tearDown(self):
        upd.os.name = self._orig_os_name
        upd.RESTART_SETTLE_SECONDS, upd.RESTART_SPAWN_SECONDS = self._saved
        super().tearDown()

    @staticmethod
    def _rc(code, out=""):
        return subprocess.CompletedProcess([], code, stdout=out, stderr="")

    def make_schtasks(self, present=None, run_fail=()):
        present = present if present is not None else set(upd.RESIDENT_TASKS)
        calls = []

        def runner(argv):
            calls.append(list(argv))
            action, name = argv[0], argv[2]
            if action == "/query":
                return self._rc(0 if name in present else 2)
            if action == "/run":
                return self._rc(1 if name in run_fail else 0)
            return self._rc(0)

        return runner, calls

    def make_lister(self, seq):
        """按脚本名返回进程快照序列：seq = {script: [snapshot, snapshot, ...]}，
        超出长度则重复最后一个快照。"""
        state = {k: list(v) for k, v in seq.items()}
        seen = {}

        def lister(script):
            snaps = state.get(script, [[]])
            i = min(seen.get(script, 0), len(snaps) - 1)
            seen[script] = seen.get(script, 0) + 1
            return snaps[i]

        return lister

    def test_never_calls_end(self):
        """回归锁：/end 对分离启动的任务是空转（且会误报成功），必须不再调用。"""
        runner, calls = self.make_schtasks()
        upd.restart_resident(runner=runner, lister=lambda s: [],
                             killer=lambda p: None)
        self.assertNotIn("/end", [c[0] for c in calls])

    def test_kills_old_pids_then_verifies_new_process(self):
        killed = []
        lister = self.make_lister({
            "worker_agent.py": [[(111, "t0")], [(222, "t1")]],
            "cp_server.py": [[(333, "t0")], [(444, "t1")]],
        })
        runner, calls = self.make_schtasks()
        results = upd.restart_resident(runner=runner, lister=lister,
                                       killer=killed.append)
        self.assertEqual(sorted(killed), [111, 333])
        self.assertIn("ok", results["code-loop-worker-agent"])
        self.assertIn("222", results["code-loop-worker-agent"])
        self.assertIn("444", results["code-loop-cp-serve"])
        for task in upd.RESIDENT_TASKS:
            self.assertIn("/run", [c[0] for c in calls if c[2] == task])

    def test_false_success_is_impossible(self):
        """本次生产缺陷的回归锁：/run 退出码 0 但进程没换 → 必须报 fail，
        而不是像旧实现那样凭任务状态报 ok。"""
        lister = self.make_lister({
            "worker_agent.py": [[(111, "t0")]],       # 旧进程一直在，从没换新
            "cp_server.py": [[(333, "t0")], [(444, "t1")]],
        })
        runner, _ = self.make_schtasks()
        results = upd.restart_resident(runner=runner, lister=lister,
                                       killer=lambda p: None)
        self.assertIn("fail", results["code-loop-worker-agent"])
        self.assertIn("未观察到新进程", results["code-loop-worker-agent"])
        self.assertIn("ok", results["code-loop-cp-serve"])

    def test_task_never_started_still_requires_new_process(self):
        """任务在但进程没跑（如刚崩过）：/run 后仍须看到新进程才算成功。"""
        lister = self.make_lister({"worker_agent.py": [[], [(222, "t1")]]})
        runner, _ = self.make_schtasks(present={"code-loop-worker-agent"})
        results = upd.restart_resident(runner=runner, lister=lister,
                                       killer=lambda p: None)
        self.assertIn("ok", results["code-loop-worker-agent"])
        self.assertIn("无", results["code-loop-worker-agent"])  # 旧 pid 为"无"

    def test_missing_task_skipped_without_run(self):
        runner, calls = self.make_schtasks(present={"code-loop-worker-agent"})
        results = upd.restart_resident(runner=runner,
                                       lister=lambda s: [], killer=lambda p: None)
        self.assertIn("skipped", results["code-loop-cp-serve"])
        self.assertNotIn("/run", [c[0] for c in calls if c[2] == "code-loop-cp-serve"])

    def test_run_failure_recorded(self):
        runner, _ = self.make_schtasks(run_fail=("code-loop-worker-agent",))
        results = upd.restart_resident(runner=runner, lister=lambda s: [],
                                       killer=lambda p: None)
        self.assertIn("fail", results["code-loop-worker-agent"])

    def test_non_windows_skips_all(self):
        real_os = upd.os.name
        upd.os.name = "posix"
        try:
            results = upd.restart_resident(runner=self.make_schtasks()[0],
                                           lister=lambda s: [],
                                           killer=lambda p: None)
        finally:
            upd.os.name = real_os
        self.assertTrue(all("skipped" in v for v in results.values()))

    def test_parse_ps_pids_ignores_noise(self):
        text = "  \r\n1234|2026-09-23T09:42:37.000+08:00\r\nnotapid|x\r\n"
        self.assertEqual(upd._parse_ps_pids(text),
                         [(1234, "2026-09-23T09:42:37.000+08:00")])


class TestMainEndToEnd(UpdateDeployTestBase):
    def run_main(self, *extra):
        return upd.main(["--root", str(self.fx.deploy), *extra])

    def test_full_update_success(self):
        self.fx.advance_origin()
        before = _git(self.fx.deploy, "rev-parse", "HEAD")
        rc = self.run_main("--no-restart")
        self.assertEqual(rc, 0)
        self.assertNotEqual(_git(self.fx.deploy, "rev-parse", "HEAD"), before)
        lines = self.journal_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["result"], "updated")
        self.assertEqual(lines[0]["gate"], "ok")

    def test_up_to_date_short_circuits_without_journal(self):
        rc = self.run_main("--no-restart")
        self.assertEqual(rc, 0)
        self.assertEqual(self.journal_lines(), [])

    def test_dirty_refuses_then_force_updates(self):
        (self.fx.deploy / "README.md").write_text("dirty", encoding="utf-8")
        self.fx.advance_origin()
        self.assertEqual(self.run_main("--no-restart"), 1)
        self.assertEqual(self.journal_lines(), [])
        self.assertEqual(self.run_main("--force", "--no-restart"), 0)
        self.assertEqual(self.journal_lines()[0]["result"], "updated")

    def test_gate_failure_exit2_loud_mismatch(self):
        self.fx.advance_origin(filename="services/code-loop/codeloop/bad.py", text="def (:\n")
        rc = self.run_main("--no-restart")
        self.assertEqual(rc, 2)
        lines = self.journal_lines()
        self.assertEqual(lines[0]["result"], "gate-failed")
        # 错位态语义：磁盘已前进到新代码，但记录 gate 失败且未重启
        self.assertNotIn("main", _git(self.fx.deploy, "status", "--porcelain"))
        lines_origin = _git(self.fx.seed, "rev-parse", "origin/main")
        self.assertEqual(_git(self.fx.deploy, "rev-parse", "HEAD"), lines_origin)

    def test_ahead_refuses(self):
        (self.fx.deploy / "local.txt").write_text("x", encoding="utf-8")
        _git(self.fx.deploy, "add", "-A")
        _git(self.fx.deploy, "commit", "-m", "local commit")
        self.assertEqual(self.run_main("--no-restart"), 1)
        self.assertEqual(self.journal_lines(), [])

    def test_check_reports_without_mutating(self):
        self.fx.advance_origin()
        rc = self.run_main("--check")
        self.assertEqual(rc, 0)
        self.assertEqual(self.journal_lines(), [])
        state = upd.survey(Path(self.fx.deploy), fetch=False)
        self.assertEqual(state["behind"], 1)  # --check 不改工作区

    def test_fetch_failure_mid_update_exits_1_cleanly(self):
        # survey 与锁内 fetch 之间网络抖动：必须干净 exit 1（不打栈、不写台账、
        # HEAD 不动、锁已释放）。
        self.fx.advance_origin()
        before = _git(self.fx.deploy, "rev-parse", "HEAD")
        real = upd.fast_forward

        def boom(root):
            raise upd.DeployError("simulated fetch failure")

        upd.fast_forward = boom
        try:
            rc = self.run_main("--no-restart")
        finally:
            upd.fast_forward = real
        self.assertEqual(rc, 1)
        self.assertEqual(_git(self.fx.deploy, "rev-parse", "HEAD"), before)
        self.assertEqual(self.journal_lines(), [])
        self.assertFalse(self.lockfile.is_file())  # 锁已释放


if __name__ == "__main__":
    unittest.main()
