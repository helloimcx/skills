#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CP 状态空间枚举器 CLI。

用法（仓库任意位置）：
  python services/code-loop/scripts/state_space.py report [--json]
  python services/code-loop/scripts/state_space.py check

report 输出确定性分类报告（无时间戳、逐字节可复现）；check 对
未裁决 UNDEFINED 格子 / 遮蔽冲突 / 陈旧台账条目 / 代码↔表漂移 /
未裁决无码 raise 任一存在即非零退出。测试
tests/test_state_space.py 内建同样的检查——本 CLI 供人用与 CI 用。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeloop.cp import state_rules as sr
from codeloop.cp import state_space as ss


def _drift_findings(scan):
    """代码理由码 ↔ 表（规则 reason ∪ 文档桶 ∪ 豁免）双向差集。"""
    covered = set(sr.DOCUMENTED_CODES) | set(sr.INPUT_VALIDATION_EXEMPT) | set(sr.NON_REASON_LITERALS)
    covered |= {r.reason for r in sr.RULES if r.reason and r.reason != sr.UNCODED_GUARD}
    scanned = set(scan.codes)
    out = []
    if scanned - covered:
        out.append('DRIFT: 代码理由码未入表/未豁免: %s'
                   % ', '.join(sorted(scanned - covered)))
    if covered - scanned:
        out.append('DRIFT: 表内理由码代码中不存在: %s'
                   % ', '.join(sorted(covered - scanned)))
    return out


def _uncoded_findings(scan):
    bad, guards = [], []
    for fname, lineno, msg in scan.uncoded:
        note = sr.disposition_for(msg)
        if note is None:
            bad.append('UNCODED: %s:%d %r 未裁决' % (fname, lineno, msg))
        elif str(note).startswith('guard'):
            guards.append('UNCODED-GUARD: %s:%d（状态守卫缺理由码）' % (fname, lineno))
    return bad, guards


def check():
    scan = sr.scan_cp_sources()
    result = ss.classify(sr.build(), review=sr.load_review())
    failures = list(result.failures) + _drift_findings(scan)
    failures += _uncoded_findings(scan)[0]
    return result, scan, failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('mode', choices=('report', 'check'))
    parser.add_argument('--json', action='store_true', help='report 模式输出 JSON')
    args = parser.parse_args(argv)
    if args.mode == 'report':
        result = ss.classify(sr.build(), review=sr.load_review())
        if args.json:
            payload = {
                'summary': result.summary(),
                'cells': [{'key': c.key, 'decision': c.decision,
                           'rule': c.rule.id if c.rule else None,
                           'reason': c.reason, 'invariant': c.invariant}
                          for c in result.cells],
                'undefined': [c.key for c in result.undefined],
            }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        else:
            sys.stdout.write(ss.render_report(result))
        return 0
    result, scan, failures = check()
    for line in failures:
        print(line)
    print('check: %s（%d 格 / %d 动作 / %d 失败）' % (
        'OK' if not failures else 'FAIL', len(result.cells),
        len(result.summary()), len(failures)))
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
