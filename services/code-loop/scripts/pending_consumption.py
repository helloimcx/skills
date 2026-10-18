#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pending_consumption.py — handler 轮次确定性输入：待消费清单（issue #94）。

列出「已打标 [❓needs-human] 且最新发言来自用户」的 open Issue——用户已经拍板、
handler 尚未消费的决策。dashboard 的 `pending_decisions` 恰好把这些行排除
（它假设球在 handler 一侧），而 handler 侧此前没有对应视图，用户回复于是掉进缝里：
2026-09-11 实证 ai_quality_analysis #7 用户回复「B」后 20+ 轮未被消费。

判据（确定性，不依赖会话重读全部 WAIT_HUMAN 评论）：
  用户最后发言时间 > agent 最后发言时间，且 Issue 仍 open、仍带 needs-human 标记
  （决策被消费后 handler 会去标）。用户发言 = 平台无身份标记评论 ∪ dashboard 代理回复。

Usage:
  python pending_consumption.py [--db PATH] [--project org__repo] [--json]

退出码:
  0  正常（清单可能为空）
  2  环境错误（store 不存在 / 项目不存在）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop.dash import collect, store  # noqa: E402


def _fmt_ts(epoch):
    if epoch is None:
        return '?'
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec='seconds')


def _fmt_wait(seconds):
    if seconds is None:
        return '?'
    if seconds >= 86400:
        return '%.1f 天' % (seconds / 86400.0)
    return '%.1f 小时' % (seconds / 3600.0)


def _row_view(issue):
    return {'org_repo': issue['org_repo'], 'number': issue['number'],
            'title': issue['title'], 'user_reply_at': _fmt_ts(issue['user_reply_at']),
            'agent_reply_at': _fmt_ts(issue['agent_reply_at']),
            'waiting_seconds': issue['waiting_seconds'],
            'html_url': issue['html_url']}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='pending_consumption',
        description='handler 待消费清单：needs-human 且用户最后发言的 open Issue（issue #94）')
    ap.add_argument('--db', default=str(collect.DEFAULT_DB), help='dashboard SQLite 路径')
    ap.add_argument('--project', default=None, help='限定 org__repo（默认全部项目）')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print('dashboard store 不存在: %s（先跑 dashboard.py collect）' % db_path,
              file=sys.stderr)
        return 2
    db = store.connect(db_path)
    try:
        if args.project and db.execute('SELECT 1 FROM projects WHERE org_repo=?',
                                       (args.project,)).fetchone() is None:
            print('项目不存在于 store: %s' % args.project, file=sys.stderr)
            return 2
        rows = store.pending_consumption(db, now=time.time())
        if args.project:
            rows = [r for r in rows if r['org_repo'] == args.project]
        freshness = [(r['org_repo'], r['last_ok_collect_at'])
                     for r in db.execute('SELECT org_repo, last_ok_collect_at FROM projects '
                                         'ORDER BY org_repo')]
    finally:
        db.close()

    if args.json:
        print(json.dumps({'pending': [_row_view(r) for r in rows],
                          'store': str(db_path),
                          'collected': [{'org_repo': k, 'last_ok_collect_at': _fmt_ts(v)}
                                        for k, v in freshness]},
                         ensure_ascii=False, indent=2))
        return 0

    scope = args.project or '全部项目'
    print('待消费清单（needs-human 且用户最后发言，%s）: %d 条' % (scope, len(rows)))
    for r in rows:
        print('  %s#%s  等待 %s  用户最后发言 %s'
              % (r['org_repo'], r['number'], _fmt_wait(r['waiting_seconds']),
                 _fmt_ts(r['user_reply_at'])))
        print('      %s' % r['title'])
    if not rows:
        print('  （空——无待消费决策；仍须按 §2 自愈检查打标/清单缺失）')
    print('数据源: %s' % db_path)
    for org_repo, last_ok in freshness:
        print('  采集新鲜度 %s: %s' % (org_repo, _fmt_ts(last_ok)))
    return 0


if __name__ == '__main__':
    sys.exit(main())