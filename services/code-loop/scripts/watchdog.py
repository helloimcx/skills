#!/usr/bin/env python3
"""常驻进程看门狗：cp-server / worker-agent 缺失即拉起（09-28 双停摆事故修复）。

事故：2026-09-28 本机 cp-server（8722/8712）与 worker agent 双双停摆约 2.5 小时，
根因=两者都是「登陆时」schtasks 触发，进程中途死亡后不会自动拉起；worker agent
虽有连接层退避重连，但只覆盖「服务端不可达」，进程本身死亡时重连逻辑无处运行。

本脚本是轻量确定性巡检（挂 schtasks 周期任务，建议每 5 分钟），对每个常驻任务：

1. 按命令行定位真正在跑的进程（幂等判据，进程在则跳过）；
2. 进程缺失 → 按标准通道拉起（`schtasks /run`，即 onlogon 启动器的同一通道）；
3. 拉起后验证「出现新进程」+（cp-server 另验 8712 HTTP 探活）才算成功；
4. 每轮结果写留痕日志（watchdog.jsonl），失败 exit 非零供调度层感知。

设计契约：
- 只拉起、永不 kill：进程在（哪怕失联）就跳过——kill 是 update_deploy 部署重启
  的职责，本看门狗只管「缺了补上」，避免误杀半死但可自愈的进程。
- 探活只作 cp-server 的补充信号：进程在 + HTTP 通才算健康；HTTP 失败但进程在 →
  本轮报 warning（不自动 kill/重启，留痕供人工）。
- 复用 update_deploy 的进程定位与 schtasks 封装（同一事实源，避免两套判定）。

机器本地部署（不入库）：注册一个周期 schtasks 任务（建议每 5 分钟）调用
本脚本，例如 schtasks /create /tn code-loop-watchdog /sc minute /mo 5
/task 命令 python services/code-loop/scripts/watchdog.py；或复用
cp-serve-cmd.cmd 的静默 vbs 风格启动器。回归验证用 --check 只读模式。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_deploy import RESIDENT_TASKS, _ps_processes, _schtasks

# cp-server 的附加 HTTP 探活端点（dashboard 面板，服务端本身就是 HTTP）
UI_PORT = 8712
UI_HEALTH_PATH = "/api/overview"
UI_PROBE_TIMEOUT = 8.0
# 拉起后验证窗口
SPAWN_WAIT_SECONDS = 20.0
SPAWN_POLL_SECONDS = 0.5


def watchdog_log_path() -> Path:
    return Path.home() / ".code-loop" / "logs" / "watchdog.jsonl"


def _append_log(entry: dict):
    path = watchdog_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _ui_healthy(timeout=UI_PROBE_TIMEOUT):
    """8712 面板 HTTP 探活：任何 <500 响应即视为服务进程在监听。

    dashboard serve 是纯 HTTP（8722 才是 TLS WS 协调面），故用 http:// 探活；
    https 探活会因 TLS 握手失败误报 down。
    """
    url = "http://127.0.0.1:%d%s" % (UI_PORT, UI_HEALTH_PATH)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def _spawn_one(task: str, script: str, runner, lister, sleeper,
               wait_seconds=SPAWN_WAIT_SECONDS) -> str:
    """进程缺失时的拉起通道：schtasks /run → 轮询新进程出现。

    与 update_deploy._restart_one 的差别：这里不 kill 任何东西（调用方已确认
    进程缺失），只做 /run + 新进程验证。返回人类可读结果串。
    """
    run = runner(["/run", "/tn", task])
    if run.returncode != 0:
        return "fail (/run rc=%d: %s)" % (run.returncode, run.stderr.strip()[:200])
    deadline = time.monotonic() + wait_seconds
    fresh = []
    while time.monotonic() < deadline:
        fresh = lister(script)
        if fresh:
            break
        sleeper(SPAWN_POLL_SECONDS)
    if not fresh:
        return "fail (/run 后未见新进程——启动器可能未注册或崩溃)"
    return "ok (new pid %s)" % fresh[0][0]


def _check_one(task: str, script: str, runner=_schtasks, lister=_ps_processes,
               sleeper=time.sleep, wait_seconds=SPAWN_WAIT_SECONDS,
               check_only=False) -> dict:
    """单任务巡检：报告当前状态；进程缺失时拉起并验证。

    check_only=True（--check 只读模式）：缺失只报告、绝不 spawn——
    启动器通道（schtasks /run）在巡检场景下一次也不应被触碰。
    """
    entry = {"task": task, "script": script,
             "time": datetime.now().isoformat(timespec="seconds")}
    pids = lister(script)
    entry["alive"] = bool(pids)
    entry["pids"] = [pid for pid, _ in pids]
    if not pids:
        if check_only:
            entry["action"] = "report"
            entry["result"] = "missing (只读模式不拉起)"
            entry["ok"] = False
        else:
            entry["action"] = "spawn"
            entry["result"] = _spawn_one(task, script, runner, lister, sleeper,
                                         wait_seconds)
            entry["ok"] = entry["result"].startswith("ok")
    else:
        entry["action"] = "none"
        entry["ok"] = True
    if task == "code-loop-cp-serve":
        entry["ui_healthy"] = _ui_healthy()
        if entry["ok"] and not entry["ui_healthy"]:
            entry["ok"] = False
            entry["warning"] = "进程在但 8712 探活失败"
    entry["exit"] = 0 if entry["ok"] else 1
    _append_log(entry)
    return entry


def _summarize(entries) -> str:
    parts = []
    for entry in entries:
        if entry["alive"]:
            base = "alive pid=%s" % ",".join(map(str, entry["pids"]))
        else:
            base = "missing -> %s %s" % (entry["action"], entry["result"])
        if entry.get("ui_healthy") is not None:
            base += " ui=%s" % ("ok" if entry["ui_healthy"] else "down")
        if entry.get("warning"):
            base += " !! " + entry["warning"]
        parts.append("%s: %s" % (entry["task"], base))
    return "; ".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="只读巡检（只报告当前状态，缺失也不拉起）")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("[watchdog] 非 win32，跳过（本机为 Windows schtasks 部署）")
        return 0

    results = []
    for task, script in RESIDENT_TASKS.items():
        results.append(_check_one(task, script, check_only=args.check))
    print("[watchdog] %s" % _summarize(results))

    failed = [entry for entry in results
              if not entry["ok"] or entry.get("warning")]
    if args.check:
        # 只读模式：报告即可，不因巡检发现缺失而以失败退出
        return 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
