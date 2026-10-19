"""report coverage 字段的结构校验与评论渲染（reviewer 判词覆盖度核算）。

校验只做**内部一致性**复核（reviewed/skipped 不相交、|reviewed|+|skipped|==
total、跳过必带理由、rate==派生值）；git 事实（total/missing）由评审者在
worktree 跑 review_coverage.py 交叉核对后原样填入，信任模型同 verify evidence。
"""
from __future__ import annotations


def _bad_shape(coverage):
    total = coverage.get('total')
    reviewed = coverage.get('reviewed')
    skipped = coverage.get('skipped')
    ok = coverage.get('ok')
    rate = coverage.get('rate')
    # bool 是 int/float 子类，必须排除：total 严格 int、rate 严格 float。
    return not (type(total) is int and total >= 0
                and isinstance(reviewed, list) and isinstance(skipped, list)
                and type(ok) is bool
                and type(rate) is float and 0.0 <= rate <= 1.0)


def _bad_reviewed(reviewed):
    seen = set()
    for path in reviewed:
        if not isinstance(path, str) or not path.strip() or path in seen:
            return True
        seen.add(path)
    return False


def _bad_skipped(skipped):
    seen = set()
    for entry in skipped:
        if not isinstance(entry, dict) or set(entry) != {'path', 'reason'}:
            return True
        path, reason = entry['path'], entry['reason']
        if not isinstance(path, str) or not path.strip() or path in seen:
            return True
        if not isinstance(reason, str) or not reason.strip():
            return True
        seen.add(path)
    return False


def validate_coverage(coverage):
    """结构 + 内部一致性校验；违反抛 ValueError('INVALID_COVERAGE')。"""
    if not isinstance(coverage, dict):
        raise ValueError('INVALID_COVERAGE')
    if _bad_shape(coverage) or _bad_reviewed(coverage['reviewed']) \
            or _bad_skipped(coverage['skipped']):
        raise ValueError('INVALID_COVERAGE')
    if set(coverage['reviewed']) & {s['path'] for s in coverage['skipped']}:
        raise ValueError('INVALID_COVERAGE')
    if len(coverage['reviewed']) + len(coverage['skipped']) != coverage['total']:
        raise ValueError('INVALID_COVERAGE')
    expected = round(len(coverage['reviewed']) / coverage['total'], 4) \
        if coverage['total'] else 1.0
    if abs(coverage['rate'] - expected) > 1e-9:
        raise ValueError('INVALID_COVERAGE')


def coverage_line(coverage):
    """有 coverage 时渲染覆盖度行（确定性，publish 时从结构化字段生成）。"""
    if not coverage:
        return ''
    return '\nCoverage: %d/%d reviewed (%d%%), %d skipped' % (
        len(coverage['reviewed']), coverage['total'],
        round(coverage['rate'] * 100), len(coverage['skipped']))
