#!/usr/bin/env python3
"""部署副本确定性更新器：把本机 skills 仓检出安全地更新到 origin/main。

检出即部署（agent-cmd.cmd: "pull of D:\\ai\\skills deploys automatically"），
本脚本把"人肉 git pull + 重启"收口成一条确定性命令：无变化秒退、增量
ff-only 拉取（永不 force/reset）、原子锁防并发、激活前完整性冒烟、激活后
重启 schtasks 常驻任务；失败路径保持旧版本继续服务或以 exit 2 大声报告
"磁盘新、进程旧"的错位态。设计契约见 docs/specs/2026-09-22-deploy-updater.md。
"""
import argparse
import json
import os
import py_compile
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MAIN_REF = "origin/main"
# 常驻任务 → 其启动器实际拉起的脚本名（用于按命令行定位真实进程）。
# 任务实例不持有这些进程：启动器是 Run(..., 0, False) 分离启动，见 restart_resident。
RESIDENT_TASKS = {
    "code-loop-worker-agent": "worker_agent.py",
    "code-loop-cp-serve": "cp_server.py",
}
RESTART_SETTLE_SECONDS = 20.0   # 等旧进程退出
RESTART_SPAWN_SECONDS = 20.0    # 等新进程出现
RESTART_POLL_SECONDS = 0.5
IMPORT_SMOKE_MODULES = ("codeloop.agent", "codeloop.cp.store", "codeloop.dash.store",
                        "codeloop.operator")
PIDLESS_GRACE_SECONDS = 10  # 无 pid 锁的宽限期（并发实例 mid-write 窗口）
SERVICE_REL = Path("services") / "code-loop"  # 本脚本所属 service 在仓库内的相对路径


class DeployError(Exception):
    """预检/锁/定位失败——exit 1，未动任何状态。"""


def journal_path() -> Path:
    return Path.home() / ".code-loop" / "logs" / "update-deploy.jsonl"


def lock_path() -> Path:
    return Path.home() / ".code-loop" / "locks" / "update-deploy.lock"


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _default_repo_root() -> Path:
    """部署单元是**仓库检出**（git 操作跨整个工作树），所以默认根取 git
    toplevel，而不是脚本所在的 service 子目录（后者没有 .git）。"""
    script_dir = _script_dir()
    proc = subprocess.run(["git", "-C", str(script_dir), "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise DeployError(f"无法从 {script_dir} 定位 git 仓库根："
                          f"{(proc.stderr or proc.stdout).strip()[:200]}")
    return Path(proc.stdout.strip()).resolve()


def resolve_root(root_arg=None) -> Path:
    """→ 仓库根。守卫：必须是真实检出（.git 为目录，linked worktree 的 .git
    是文件 → 拒绝）且含本 service 布局。"""
    root = Path(root_arg).resolve() if root_arg else _default_repo_root()
    git_meta = root / ".git"
    if not git_meta.exists():
        raise DeployError(f"{root}: 不是 git 检出（无 .git）")
    if git_meta.is_file():
        raise DeployError(
            f"{root}: .git 是文件——这是 linked worktree（loop/人工任务 worktree），"
            "不是部署副本，禁止在此更新")
    if not (root / SERVICE_REL / "codeloop").is_dir():
        raise DeployError(
            f"{root}: 未找到 {SERVICE_REL}/codeloop——不是本仓库的部署检出"
            f"（--root 传的是仓库根，不是 {SERVICE_REL} 子目录）")
    return root


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        raise DeployError(
            f"git {' '.join(args)} 失败 rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[:400]}")
    return proc.stdout


def survey(root: Path, fetch: bool = True) -> dict:
    if fetch:
        _git(root, "fetch", "origin")
    head = _git(root, "rev-parse", "HEAD").strip()
    main = _git(root, "rev-parse", MAIN_REF).strip()
    behind = int(_git(root, "rev-list", "--count", f"HEAD..{MAIN_REF}").strip() or 0)
    ahead = int(_git(root, "rev-list", "--count", f"{MAIN_REF}..HEAD").strip() or 0)
    dirty = [line for line in _git(root, "status", "--porcelain").splitlines()
             if line.strip() and not line.startswith("??")]
    return {"head": head, "main": main, "behind": behind,
            "ahead": ahead, "dirty": dirty}


def classify(state: dict, force: bool):
    """→ (verdict, reason)。verdict ∈ up-to-date / update / refuse。"""
    if state["behind"] == 0 and state["ahead"] == 0:
        return "up-to-date", f"HEAD 已是 {MAIN_REF}"
    if state["ahead"] > 0:
        return "refuse", ("部署副本本地领先 main（有本地提交）——"
                          "是否保留请人工决定，更新器不覆盖")
    if state["dirty"] and not force:
        return "refuse", ("存在未提交的 tracked 改动（--force 可跳过，"
                          f"与更新冲突的文件仍会被 git 拒绝）：{state['dirty'][:5]}")
    return "update", f"behind={state['behind']}"


class UpdateLock:
    """原子锁：O_EXCL 创建 + pid 写入 + holder 验尸接管。接管语义对齐
    dash/collect.py 的 issue #187/#205 结论：存活 holder 永不被接管；pid 可解析
    且已死 → 接管；pid 不可解析（刚创建、pid 未落盘的瞬间）→ 仅在超过
    PIDLESS_GRACE 后才接管，否则视为"正在写入"而拒绝，避免双写。"""

    def __init__(self, path: Path):
        self.path = path

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._take_over_or_fail()
            return self.acquire()
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))

    def _take_over_or_fail(self):
        raw = ""
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            pid = int(raw)
        except (OSError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            raise DeployError(f"更新锁被占用：{self.path}"
                              f"（holder pid={pid} 仍存活）——另一个更新器正在运行")
        if not pid:
            # 不可解析：可能是并发实例刚 O_EXCL 创建、pid 尚未落盘的瞬间。
            # 只有超过宽限期仍无 pid 才判为遗留坏锁（issue #205 语义）。
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                age = 0
            if age < PIDLESS_GRACE_SECONDS:
                raise DeployError(
                    f"更新锁被占用：{self.path}（pid 尚未落盘，age={age:.1f}s < "
                    f"{PIDLESS_GRACE_SECONDS}s）——可能有并发更新器正在启动")
        self.path.unlink(missing_ok=True)  # 死 pid 或超龄坏锁：接管

    def release(self):
        """只释放自己的锁：被接管过的运行不得删除新 holder 的锁。"""
        try:
            if self.path.read_text(encoding="utf-8").strip() != str(os.getpid()):
                return
        except OSError:
            return
        self.path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """win32 上 os.kill(pid, 0) 会 TERMINATE 进程，必须走 OpenProcess 探测；
    ERROR_ACCESS_DENIED 说明进程存在但无权查询。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        handle = k32.OpenProcess(0x1000 | 0x00100000, False, int(pid))
        if handle:
            k32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False


def fast_forward(root: Path):
    _git(root, "fetch", "origin")  # 不依赖调用方先 survey；ff 判据必须基于最新远端
    frm = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "merge", "--ff-only", MAIN_REF)
    to = _git(root, "rev-parse", "HEAD").strip()
    return frm, to


def run_activation_gate(repo_root: Path, modules=None) -> list:
    """激活前三件（作用于 <repo>/services/code-loop）：py_compile 全部
    codeloop/**/*.py + import 冒烟（子进程隔离，防 sys.modules 污染）+
    state_space check（确定性回归金丝雀）。返回失败清单。"""
    if modules is None:  # 默认值调用时读取，测试才能 monkeypatch
        modules = IMPORT_SMOKE_MODULES
    root = repo_root / SERVICE_REL
    failures = []
    for path in sorted((root / "codeloop").rglob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"compile {path.relative_to(root)}: {exc}")
    if failures:
        return failures
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    # 子进程输出确定按 UTF-8：宿主可能是 cp936 控制台（或反过来设了 PYTHONUTF8），
    # 显式钉扎后下方 utf-8 解码在任何机器上都成立。
    env["PYTHONIOENCODING"] = "utf-8"
    smoke = "import importlib; [importlib.import_module(m) for m in %r]" % (modules,)
    proc = subprocess.run([sys.executable, "-c", smoke], cwd=str(root), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode != 0:
        failures.append(
            f"import smoke {modules} rc={proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()[-400:]}")
    state_space = root / "scripts" / "state_space.py"
    if not state_space.is_file():
        failures.append("scripts/state_space.py 缺失（激活门金丝雀不可用）")
        return failures
    proc = subprocess.run([sys.executable, str(state_space), "check"],
                          cwd=str(root), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        failures.append(f"state_space check rc={proc.returncode}: "
                        f"{(proc.stdout or proc.stderr).strip()[-400:]}")
    return failures


def _decode_console(raw: bytes) -> str:
    """schtasks 输出走系统 ANSI/OEM 代码页（中文 Windows = cp936）：实测按 UTF-8
    解码直接失败、mbcs 正确。注意不能用 locale.getpreferredencoding——宿主设了
    PYTHONUTF8=1 时它返回 utf-8，与实际字节不符。"""
    for enc in ("mbcs", "utf-8") if os.name == "nt" else ("utf-8",):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _schtasks(argv):
    proc = subprocess.run(["schtasks", *argv], capture_output=True)
    return subprocess.CompletedProcess(proc.args, proc.returncode,
                                       _decode_console(proc.stdout),
                                       _decode_console(proc.stderr))


def _parse_ps_pids(text: str) -> list:
    """解析 `pid|start_iso` 行，返回 [(pid, start_iso)]。"""
    found = []
    for line in text.splitlines():
        pid_text, sep, start = line.strip().partition("|")
        if sep and pid_text.strip().isdigit():
            found.append((int(pid_text), start.strip()))
    return found


def _ps_processes(script_name: str) -> list:
    """本机命令行含该脚本名的 python 进程 [(pid, start_iso)]。

    用 PowerShell 的 CIM 查询而非 wmic（后者在新版 Windows 已移除）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { \"$($_.ProcessId)|$($_.CreationDate.ToString('o'))\" }"
          % script_name)
    proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                          capture_output=True)
    return _parse_ps_pids(_decode_console(proc.stdout))


def _kill_pid(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def _poll_until(probe, sleeper, timeout_seconds, interval=RESTART_POLL_SECONDS):
    """轮询 probe 直到返回真值或超时；返回 probe 的最后结果。"""
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        result = probe()
        if result or time.monotonic() >= deadline:
            return result
        sleeper(interval)


def _restart_one(task: str, script: str, runner, lister, killer, sleeper) -> str:
    if runner(["/query", "/tn", task]).returncode != 0:
        return "skipped (任务未注册)"
    old_pids = {pid for pid, _ in lister(script)}
    for pid in old_pids:
        killer(pid)
    _poll_until(lambda: not (old_pids & {p for p, _ in lister(script)}),
                sleeper, RESTART_SETTLE_SECONDS)
    run = runner(["/run", "/tn", task])
    if run.returncode != 0:
        return f"fail (/run rc={run.returncode}: {run.stderr.strip()[:200]})"
    fresh = _poll_until(
        lambda: [(p, s) for p, s in lister(script) if p not in old_pids] or None,
        sleeper, RESTART_SPAWN_SECONDS)
    if not fresh:
        return "fail (重启后未观察到新进程——/run 空转，进程仍是旧的或已缺失)"
    old_label = ",".join(str(p) for p in sorted(old_pids)) or "无"
    return f"ok (pid {old_label} → {fresh[0][0]})"


def restart_resident(runner=_schtasks, lister=_ps_processes, killer=_kill_pid,
                     sleeper=time.sleep):
    """重启常驻任务：杀掉真正在跑的进程 → /run → 验证新进程真的出现。

    这些任务的启动器（`~/.code-loop/*-silent.vbs`）用 `Run(..., 0, False)`
    **分离启动**，任务实例在 wscript 退出瞬间就结束了（任务状态是"就绪"而非
    "正在运行"），python 是脱离任务实例的**孤儿进程**。由此（2026-09-23 生产
    实测）：

    - `schtasks /end` 没有对象可终止，**永远杀不掉这些进程**——它却会报
      "已被成功终止"（schtasks 层的假成功）；
    - 任务状态与 `/run` 退出码都**无法证明**重启生效：实测 /end+/run 之后旧
      进程的 pid 与启动时间原封不动，而状态串在 /run 后一瞬间还显示"正在运行"，
      于是工具报 `ok`——重启其实是空转。

    唯一可信的判据是**进程身份**：旧 pid 消失、且出现新 pid。因此这里按脚本名
    定位进程、直接 taskkill，再 /run，并以"是否观察到新进程"判定成败。
    """
    if os.name != "nt":
        return {name: "skipped (非 win32，请人工重启)" for name in RESIDENT_TASKS}
    return {task: _restart_one(task, script, runner, lister, killer, sleeper)
            for task, script in RESIDENT_TASKS.items()}


def append_journal(entry: dict):
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _print_state(state: dict):
    print(f"  HEAD        {state['head'][:12]}")
    print(f"  {MAIN_REF:<12}{state['main'][:12]}")
    print(f"  behind/ahead {state['behind']}/{state['ahead']}")
    if state["dirty"]:
        print(f"  dirty tracked ({len(state['dirty'])}):")
        for line in state["dirty"][:10]:
            print(f"    {line}")


def _journal_entry(frm, to, result, gate, restart) -> dict:
    return {"time": datetime.now().isoformat(timespec="seconds"),
            "from": frm, "to": to, "result": result,
            "gate": gate, "restart": restart}


def _apply_update(root: Path, state: dict, no_restart: bool) -> int:
    lock = UpdateLock(lock_path())
    try:
        lock.acquire()
    except DeployError as exc:
        print(f"[update-deploy] FAIL {exc}")
        return 1
    try:
        try:
            frm, to = fast_forward(root)
        except DeployError as exc:
            # survey 已 fetch 过，但锁内会再 fetch 一次——此间网络抖动/远端竞态
            # 会让 fetch 或 --ff-only 失败。锁已释放（finally），HEAD 未动。
            print(f"[update-deploy] FAIL 拉取失败：{exc}")
            return 1
        print(f"[update-deploy] ff-only {frm[:12]} -> {to[:12]}")

        failures = run_activation_gate(root)
        if failures:
            return _exit_gate_failed(root, frm, to, failures)

        if no_restart:
            restart = {name: "skipped (--no-restart)" for name in RESIDENT_TASKS}
        else:
            restart = restart_resident()
        for name, result in restart.items():
            print(f"  restart {name}: {result}")

        failed = [n for n, r in restart.items() if r.startswith("fail")]
        if failed:
            return _exit_restart_failed(frm, to, restart, failed)
        append_journal(_journal_entry(frm, to, "updated", "ok", restart))
        print("[update-deploy] 更新完成")
        return 0
    finally:
        lock.release()


def _exit_gate_failed(root: Path, frm, to, failures) -> int:
    print("[update-deploy] 激活门失败——进程未重启：")
    for line in failures:
        print(f"  {line}")
    print(f"[update-deploy] 磁盘已是新代码 {to[:12]}，常驻进程仍在跑旧代码；"
          f"回滚请人工执行 git -C {root} reset --hard {frm}")
    append_journal(_journal_entry(frm, to, "gate-failed", failures, "skipped"))
    return 2


def _exit_restart_failed(frm, to, restart, failed) -> int:
    print(f"[update-deploy] 重启失败 {failed}——磁盘新代码、进程可能仍是旧代码，"
          "请人工核查 schtasks")
    append_journal(_journal_entry(frm, to, "restart-failed", "ok", restart))
    return 2


def _cmd_check() -> int:
    for name in RESIDENT_TASKS:
        query = _schtasks(["/query", "/tn", name]) if os.name == "nt" else None
        present = "未注册" if (query is None or query.returncode != 0) else "已注册"
        print(f"  task {name}: {present}")
    print("[update-deploy] check 完成（只读）")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="只读报告（behind/ahead/dirty/常驻任务），不改部署状态")
    parser.add_argument("--root", help="部署根（默认脚本自推导的 service 根）")
    parser.add_argument("--force", action="store_true",
                        help="允许 dirty tracked 检出继续（分叉/本地领先仍拒绝）")
    parser.add_argument("--no-restart", action="store_true",
                        help="更新与门禁照做，跳过常驻进程重启")
    args = parser.parse_args(argv)

    try:
        root = resolve_root(args.root)
        state = survey(root)
    except DeployError as exc:
        print(f"[update-deploy] FAIL {exc}")
        return 1
    print(f"[update-deploy] 部署根 {root}")
    _print_state(state)

    if args.check:
        return _cmd_check()

    verdict, reason = classify(state, force=args.force)
    print(f"[update-deploy] {verdict}: {reason}")
    if verdict == "up-to-date":
        return 0
    if verdict == "refuse":
        return 1
    return _apply_update(root, state, no_restart=args.no_restart)


if __name__ == "__main__":
    sys.exit(main())
