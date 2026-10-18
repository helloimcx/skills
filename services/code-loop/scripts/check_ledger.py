#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_ledger.py — handler 台账反坍缩校验（issue #94）+ 数量一致性校验（issue #355）。

台账快照表约定「每个 Open Issue 一行」。历史事故里出现 `225/153/…/7 决策类 ×12`
这类批量行：批量行成员在 §2 回流中被会话跳过（批量行 = 冷存储），用户决策 20+ 轮
未被消费。本脚本确定性检出批量行，供轮次输出校验使用。

判据：
  1. 快照表数据行的首列必须是单个 Issue 号（纯数字）。首列含 `/`、`×`、`,`、`-`、
     空格或任何非数字字符一律判违规（issue #94）。
  2. 快照表行数必须与平台 open issue 数一致（issue #355）——已合并 PR 的 issue
     未从快照表移除会导致行数漂移、盘点失真。用 `--expect <N>` 显式传期望数，
     或 `--gitea` 自动查询平台 open 数。

Usage:
  python check_ledger.py [PATH] [--expect <N> | --gitea]
  # 默认 <repo>/.agents/loops/state/handler-issues.md

退出码:
  0  通过（快照表每行恰一个 Issue 号，且行数与期望一致）
  1  检出批量行 / 行数与期望不一致
  2  环境错误（文件不存在 / 找不到快照表 / gitea 查询失败）
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEFAULT_LEDGER = REPO / '.agents' / 'loops' / 'state' / 'handler-issues.md'
SNAPSHOT_HEADING = '## 快照表'
ISSUE_CELL = re.compile(r'\d+')


def check(text):
    """Scan the snapshot table. Returns (violations, rows_checked) where each
    violation is (lineno, first_cell, raw_line); None violations means the
    snapshot table itself is missing."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip().startswith(SNAPSHOT_HEADING)), None)
    if start is None:
        return None, 0
    violations, checked, started = [], 0, False
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped.startswith('|'):
            if started:
                break
            continue  # blank line between the heading and the table
        cells = [c.strip() for c in stripped.strip('|').split('|')]
        first = cells[0] if cells else ''
        if not first or set(first) <= set('-: '):
            continue  # separator row
        if first == '#':
            continue  # header row
        started = True
        checked += 1
        if not ISSUE_CELL.fullmatch(first):
            violations.append((index + 1, first, stripped))
    return violations, checked


def platform_open_count(platform='github') -> int:
    """运行 CLI 统计平台 open issue 数。

    支持 github (gh) 或 gitea。
    执行失败抛 RuntimeError。
    """
    if platform == 'github':
        cmd = _github_cmd()
        try:
            proc = subprocess.run(
                cmd + ['issue', 'list', '--state', 'open', '--limit', '1000'],
                capture_output=True, text=True, encoding='utf-8', timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('gh issue list 执行失败: %s' % exc) from exc
        if proc.returncode != 0:
            raise RuntimeError('gh issue list 退出码 %d: %s'
                               % (proc.returncode, proc.stderr.strip()[:200]))
        return sum(1 for line in proc.stdout.splitlines()
                   if re.match(r'^\s*#?\d+\s', line))

    cmd = _gitea_cmd()
    try:
        proc = subprocess.run(
            cmd + ['issue', 'list', '--state', 'open'],
            capture_output=True, text=True, encoding='utf-8', timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('gitea issue list 执行失败: %s' % exc) from exc
    if proc.returncode != 0:
        raise RuntimeError('gitea issue list 退出码 %d: %s'
                           % (proc.returncode, proc.stderr.strip()[:200]))
    return sum(1 for line in proc.stdout.splitlines()
               if re.match(r'\s*#\d+\s', line))


def _github_cmd() -> list[str]:
    """定位 GitHub CLI (gh) 命令。"""
    exe = shutil.which('gh')
    if exe:
        return [exe]
    cmd = shutil.which('gh.cmd')
    if cmd:
        return [cmd]
    raise RuntimeError('找不到 gh CLI（PATH 中无 gh / gh.cmd）')


def _gitea_cmd() -> list[str]:
    """定位 gitea CLI 命令。

    优先 PATH 中的 `gitea`（bash shim）；Windows 下 PATHEXT 看不到无扩展名
    shim，回退 `gitea.cmd`（python gitea.py 包装）。
    """
    exe = shutil.which('gitea')
    if exe:
        return [exe]
    cmd = shutil.which('gitea.cmd')
    if cmd:
        return [cmd]
    raise RuntimeError('找不到 gitea CLI（PATH 中无 gitea / gitea.cmd）')


def main(argv=None):
    ap = argparse.ArgumentParser(description='handler 台账校验（反坍缩 + 数量一致性）')
    ap.add_argument('path', nargs='?', default=None, help='台账路径（默认仓库默认台账）')
    ap.add_argument('--expect', type=int, default=None,
                    help='期望的快照表行数（= 平台 open issue 数）')
    ap.add_argument('--github', action='store_true',
                    help='自动运行 `gh issue list --state open` 统计平台 open 数')
    ap.add_argument('--gitea', action='store_true',
                    help='自动运行 `gitea issue list --state open` 统计平台 open 数')
    args = ap.parse_args(argv)

    path = Path(args.path) if args.path else DEFAULT_LEDGER
    if not path.is_file():
        print('台账不存在: %s' % path, file=sys.stderr)
        return 2
    violations, checked = check(path.read_text(encoding='utf-8'))
    if violations is None:
        print('台账缺少「%s」快照表: %s' % (SNAPSHOT_HEADING, path), file=sys.stderr)
        return 2
    if violations:
        print('台账反坍缩校验 FAIL：%d 行批量行（快照表必须每个 Issue 独立一行）'
              % len(violations), file=sys.stderr)
        for lineno, first, raw in violations:
            print('  L%d  首列=%r  %s' % (lineno, first, raw), file=sys.stderr)
        return 1

    # 数量一致性（issue #355）：快照表行数 == 平台 open issue 数
    if args.github:
        try:
            args.expect = platform_open_count('github')
        except RuntimeError as exc:
            print('数量校验失败: %s' % exc, file=sys.stderr)
            return 2
    elif args.gitea:
        try:
            args.expect = platform_open_count('gitea')
        except RuntimeError as exc:
            print('数量校验失败: %s' % exc, file=sys.stderr)
            return 2
    if args.expect is not None and checked != args.expect:
        print('台账数量校验 FAIL：快照表 %d 行 ≠ 平台 open %d 个'
              '（已合并 PR 的 issue 未从快照表移除？）' % (checked, args.expect),
              file=sys.stderr)
        return 1
    if args.expect is not None:
        print('台账反坍缩校验 PASS：快照表 %d 行，每行恰一个 Issue 号，与平台 open 数一致'
              % checked)
    else:
        print('台账反坍缩校验 PASS：快照表 %d 行，每行恰一个 Issue 号' % checked)
    return 0


if __name__ == '__main__':
    sys.exit(main())