#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review_coverage.py — reviewer 判词覆盖度核算（确定性，纯标准库）。

reviewer 的 Approved 判词必须证明自己看全了 PR diff。本脚本以 git 为唯一
事实源：``compute`` 用 ``git diff --name-status <base>...<head>`` 算出 diff
文件全集（三点 diff = head 相对 merge-base 的改动，即 PR 内容）；``check``
拿 agent 自述的 reviewed / skipped(带理由) 清单做交叉核对，missing = diff 里
既没审也没说明理由跳过的文件。记账只对 diff 文件集有意义：reviewed/skipped
中出现 diff 外路径 → ``ok=false``（与 CP 门禁 |reviewed|+|skipped|==total 一致）。

- ``--repo`` 缺省 cwd，须是 git 仓库；``--base`` / ``--head`` 为任意 git ref。
- ``auto_skip`` 只是候选提示（lockfile / vendor / 构建产物 / 生成文件），
  跳过决定与理由始终归评审者；``check`` 只要求"要么已审、要么有理由跳过"。
- 任何 git 失败（仓库不可用、ref 不存在）→ 非零退出 + stderr 错误。

用法::

    python review_coverage.py compute --base origin/main --head <sha>
    python review_coverage.py check --base origin/main --head <sha> \\
        --reviewed a.py,b.go --skip 'lock.json:lockfile;dist/:generated'
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# 内置候选跳过规则：命中即建议跳过，不强制。key=匹配的路径片段/模式。
LOCKFILES = {
    'package-lock.json', 'npm-shrinkwrap.json', 'yarn.lock', 'pnpm-lock.yaml',
    'Cargo.lock', 'poetry.lock', 'Pipfile.lock', 'composer.lock',
    'Gemfile.lock', 'go.sum',
}
SKIP_DIRS = (
    'node_modules', 'vendor', 'third_party', 'dist', 'build', 'target',
    '.venv', 'venv', 'generated', 'out', '.gradle',
)
SKIP_SUFFIXES = ('.min.js', '.min.css', '.map')


def git_lines(repo, *args):
    """``git -C repo <args>`` 的 stdout 行列表；失败抛 RuntimeError（带 stderr）。

    ``encoding='utf-8'`` + ``-c core.quotepath=false`` 保证非 ASCII 路径
    （中文等）以原文输出而非 C 转义串；30s 超时防大仓库/网络盘挂住。
    """
    proc = subprocess.run(['git', '-C', str(repo), *args],
                          capture_output=True, text=True, encoding='utf-8', timeout=30)
    if proc.returncode != 0:
        raise RuntimeError('git %s failed: %s' % (' '.join(args),
                                                  (proc.stderr or proc.stdout).strip()))
    return [line for line in proc.stdout.splitlines() if line]


def diff_files(repo, base, head):
    """[(path, status)] 按 git diff 输出顺序；rename 取新路径（PR 侧路径）。"""
    out = []
    for line in git_lines(repo, '-c', 'core.quotepath=false',
                          'diff', '--name-status', base + '...' + head):
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        out.append((parts[-1], parts[0][:1]))
    return out


def auto_skip_for(path):
    """命中内置候选规则 → 返回规则名；否则 None。"""
    name = Path(path).name
    if name in LOCKFILES or name.endswith('.lock'):
        return 'lockfile'
    if any(path.endswith(s) for s in SKIP_SUFFIXES):
        return 'generated:minified-or-map'
    dirs = Path(path).parts[:-1]
    if any(d in SKIP_DIRS for d in dirs):
        return 'generated:build-or-vendor-dir'
    return None


def cmd_compute(args):
    files = diff_files(args.repo, args.base, args.head)
    out = {
        'total': len(files),
        'files': [{'path': p, 'status': s} for p, s in files],
        'auto_skip': [{'path': p, 'rule': r} for p, _ in files
                      if (r := auto_skip_for(p)) is not None],
    }
    print(json.dumps(out, ensure_ascii=False))


def parse_reviewed(csv):
    return [p for p in (x.strip() for x in (csv or '').split(',')) if p]


def parse_skips(text):
    """``path:reason;path:reason`` → [{path, reason}]；缺冒号时 reason=''。"""
    out = []
    for item in (x.strip() for x in (text or '').split(';')):
        if not item:
            continue
        if ':' in item:
            path, reason = item.split(':', 1)
        else:
            path, reason = item, ''
        out.append({'path': path.strip(), 'reason': reason.strip()})
    return out


def cmd_check(args):
    diff = [p for p, _ in diff_files(args.repo, args.base, args.head)]
    reviewed = parse_reviewed(args.reviewed)
    skipped = parse_skips(args.skip)
    diff_set, reviewed_set = set(diff), set(reviewed)
    skipped_paths = {s['path'] for s in skipped}
    missing = [p for p in diff if p not in reviewed_set and p not in skipped_paths]
    overlap = sorted(reviewed_set & skipped_paths)
    no_reason = [s['path'] for s in skipped if not s['reason']]
    # 记账只对 diff 文件集有意义：claimed 路径必须 ⊆ diff（diff 外出现 =
    # 记账混乱，ok=false；rate 由 ok 承载拒绝，不作为独立约束）。
    outside = sorted((reviewed_set | skipped_paths) - diff_set)
    total = len(diff)
    rate = round(len(reviewed) / total, 4) if total else 1.0
    ok = (not missing and not overlap and not no_reason and not outside)
    print(json.dumps({
        'ok': ok,
        'total': total,
        'reviewed': reviewed,
        'skipped': skipped,
        'missing': missing,
        'outside': outside,
        'rate': rate,
    }, ensure_ascii=False))


def main(argv=None):
    p = argparse.ArgumentParser(description='reviewer 判词覆盖度核算')
    p.add_argument('--repo', default='.', help='git 仓库路径（缺省 cwd）')
    sub = p.add_subparsers(dest='command', required=True)

    q = sub.add_parser('compute', help='输出 diff 文件全集 + 候选跳过')
    q.add_argument('--base', required=True)
    q.add_argument('--head', required=True)

    q = sub.add_parser('check', help='交叉核对 reviewed/skipped 与 git 事实')
    q.add_argument('--base', required=True)
    q.add_argument('--head', required=True)
    q.add_argument('--reviewed', default='', help='已审路径，逗号分隔')
    q.add_argument('--skip', default='', help='path:reason;path:reason')

    args = p.parse_args(argv)
    try:
        Path(args.repo).resolve()
        if args.command == 'compute':
            cmd_compute(args)
        else:
            cmd_check(args)
    except RuntimeError as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
