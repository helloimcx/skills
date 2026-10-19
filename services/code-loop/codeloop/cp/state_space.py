# -*- coding: utf-8 -*-
"""CP 状态空间枚举器——引擎（纯机械，零 CP 依赖）。

把「(状态组合 × 协议动作)」的全空间确定性枚举一遍：每个格子要么命中
一条规则得到裁决（ALLOW / REJECT / DERIVE，带理由码与中文不变量断言），
要么是 UNDEFINED——必须由人工裁决台账显式认领（accepted / invalid /
ignored / bug），否则 check 失败。动机与规格见
docs/specs/2026-09-22-cp-state-space-enum.md。

引擎刻意不 import 任何规则数据（声明式数据在 state_rules.py）：本模块
只提供维度注册、适用面裁剪、first-match 评估、遮蔽检测、枚举分类、
理由码扫描与确定性报告。无 I/O（扫描函数读入即纯化）、无时钟、无随机。
"""
import json
import re
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

DECISIONS = ('ALLOW', 'REJECT', 'DERIVE', 'UNDEFINED')
DISPOSITION_STATUS = ('accepted', 'invalid', 'ignored', 'bug')

# 一行 raise 的字符串字面量，及其开头是否是机器可读理由码。
_RAISE_RE = re.compile(r"raise\s+\w+\s*\(\s*'([^']*)'")
_CODE_RE = re.compile(r'^([A-Z][A-Z0-9_]+)(?::|$)')
# 补充面：不以 raise 字面量首参出现的码（如 gate.py 把码作为实参传给
# _require_independent_judgment 再统一抛出）。只认带下划线或冒号引导的
# 带码字符串——纯状态字面量（'READY'/'SENT' 等无下划线）不入码集。
_ARG_CODE_RE = re.compile(r"'([A-Z][A-Z0-9]*_[A-Z0-9_]+)'|'([A-Z][A-Z0-9_]+):[^']*'")


@dataclass
class Rule:
    """一条裁决规则：``when`` 命中即按声明顺序 first-match 生效。

    ``fallback=True`` 标记动作的兜底默认规则：允许与任意具体规则重叠
    （分层前置语义），且必须是该动作的最后一条——否则它会在前面遮蔽
    具体规则，被校验为失败。非 fallback 规则之间若同格裁决不同则记遮蔽。
    """
    id: str
    action: str
    when: object            # ctx(dict) -> bool
    decision: str
    reason: object = None   # REJECT 须带代码理由码（或 UNCODED_GUARD 哨兵）
    invariant: str = ''
    fallback: bool = False
    guard: bool = False     # 前置守卫：先占位时后续分支的重叠=不可达分支，非遮蔽     # 前置守卫：先占位时后续分支的重叠=不可达分支，非遮蔽

    def __post_init__(self):
        if self.decision not in ('ALLOW', 'REJECT', 'DERIVE'):
            raise ValueError('rule %s: decision must be ALLOW/REJECT/DERIVE' % self.id)
        if self.decision == 'REJECT' and not self.reason:
            raise ValueError('rule %s: REJECT requires a reason code' % self.id)


@dataclass
class Model:
    """声明式模型：维度取值、动作适用面、结构剪枝（各带不变量）、规则表。"""
    dimensions: dict
    actions: dict
    prunings: list          # [(invalid(ctx)->bool, invariant)]，剪枝必须可陈述
    rules: list = field(default_factory=list)

    def __post_init__(self):
        for rule in self.rules:
            if rule.action not in self.actions:
                raise ValueError('rule %s: unknown action %s' % (rule.id, rule.action))
        per_action = {}
        for rule in self.rules:
            per_action.setdefault(rule.action, []).append(rule)
        for action, rules in per_action.items():
            seen_case = False
            for rule in rules:
                if rule.guard and seen_case:
                    raise ValueError(
                        'rule %s: guard rule must precede case rules of %s'
                        % (rule.id, action))
                seen_case = seen_case or (not rule.guard and not rule.fallback)
            for idx, rule in enumerate(rules[:-1]):
                if rule.fallback:
                    raise ValueError(
                        'rule %s: fallback must be the last rule of action %s '
                        '(否则遮蔽后续具体规则)' % (rule.id, action))


@dataclass
class Decision:
    """单格评估结果；UNDEFINED 时 rule 为 None。"""
    decision: str
    rule: object = None
    shadows: list = field(default_factory=list)

    @property
    def reason(self):
        return self.rule.reason if self.rule is not None else None

    @property
    def invariant(self):
        return self.rule.invariant if self.rule is not None else ''


@dataclass
class Cell:
    key: str
    action: str
    ctx: dict
    decision: str
    rule: object = None
    shadows: list = field(default_factory=list)

    @property
    def reason(self):
        return self.rule.reason if self.rule is not None else None

    @property
    def invariant(self):
        return self.rule.invariant if self.rule is not None else ''


@dataclass
class ClassifyResult:
    cells: list
    undefined: list
    shadows: list
    failures: list

    @property
    def complete(self):
        return not self.failures

    def summary(self):
        """{action: {decision: n}}，键排序固定——报告确定性的基础。"""
        out = {}
        for cell in self.cells:
            out.setdefault(cell.action, {}).setdefault(cell.decision, 0)
            out[cell.action][cell.decision] += 1
        return {a: {d: out[a][d] for d in sorted(out[a])} for a in sorted(out)}


@dataclass
class ScanResult:
    """理由码扫描：coded = {code: [(file, line)]}；uncoded = [(file, line, msg)]。"""
    codes: dict = field(default_factory=dict)
    uncoded: list = field(default_factory=list)


def cell_key(action, ctx):
    """格子稳定键：action|dim=value,…（维度名字典序）。台账以此为主键。"""
    return action + '|' + ','.join('%s=%s' % (d, ctx[d]) for d in sorted(ctx))


def contexts_for(model, action):
    """一个动作在其适用面维度上的全部结构合法组合（按声明序展开）。"""
    dims = model.actions[action]
    unknown = [d for d in dims if d not in model.dimensions]
    if unknown:
        raise ValueError('action %s: unknown dimensions %s' % (action, unknown))
    for values in product(*(model.dimensions[d] for d in dims)):
        ctx = dict(zip(dims, values))
        if any(invalid(ctx) for invalid, _ in model.prunings):
            continue
        yield ctx


def evaluate(model, action, ctx):
    """first-match 评估；非 fallback 的后续规则也命中但裁决不同 ⇒ 遮蔽（冲突）。"""
    matched = [r for r in model.rules
               if r.action == action and r.when(ctx)]
    if not matched:
        return Decision('UNDEFINED')
    first = matched[0]
    if first.guard:
        return Decision(first.decision, first, [])
    shadows = [m for m in matched[1:]
               if m.decision != first.decision and not m.fallback]
    return Decision(first.decision, first, shadows)


def _classify_action(model, action):
    """单动作全格分类；零格子（剪枝写错）与零命中规则（死规则）都是失败。"""
    cells, undefined, shadows, failures = [], [], [], []
    hits = set()
    count = 0
    for ctx in contexts_for(model, action):
        count += 1
        decision = evaluate(model, action, ctx)
        if decision.rule is not None:
            hits.add(decision.rule.id)
        cell = Cell(cell_key(action, ctx), action, ctx,
                    decision.decision, decision.rule, decision.shadows)
        cells.append(cell)
        if cell.decision == 'UNDEFINED':
            undefined.append(cell)
        if cell.shadows:
            shadows.append(cell)
            failures.append('SHADOW: %s 被 %s 遮蔽冲突'
                            % (cell.key, ','.join(r.id for r in cell.shadows)))
    if count == 0:
        failures.append('EMPTY_ACTION: %s 剪枝后零格子（适用面/剪枝谓词写错）'
                        % action)
    for rule in model.rules:
        if rule.action == action and rule.id not in hits:
            failures.append('DEAD_RULE: %s 全空间零命中（被剪枝/前置规则架空）'
                            % rule.id)
    return cells, undefined, shadows, failures


def classify(model, review=None):
    """枚举全空间并分类；台账只认 UNDEFINED 格子，陈旧条目同样是失败。"""
    review = review or {}
    for key, entry in review.items():
        if not isinstance(entry, dict) or entry.get('status') not in DISPOSITION_STATUS \
                or not str(entry.get('note') or '').strip():
            raise ValueError('invalid disposition for %s: %r' % (key, entry))
    cells, undefined, shadows, failures = [], [], [], []
    for action in model.actions:
        c, u, s, f = _classify_action(model, action)
        cells += c
        undefined += u
        shadows += s
        failures += f
    seen = set()
    for cell in undefined:
        seen.add(cell.key)
        if cell.key not in review:
            failures.append('UNDEFINED: %s 无裁决' % cell.key)
    for key in sorted(set(review) - seen):
        failures.append('STALE_DISPOSITION: %s 已非 UNDEFINED，须从台账清除' % key)
    return ClassifyResult(cells, undefined, shadows, failures)


def scan_code_reason_codes(paths):
    """扫描 raise 点：带机器可读理由码的入 codes，裸消息的入 uncoded。"""
    scan = ScanResult()
    for path in paths:
        path = Path(path)
        for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            where = '%s:%d' % (path.name, lineno)
            if 'raise' in line:
                match = _RAISE_RE.search(line)
                if match is not None:
                    captured = match.group(1)
                    code = _CODE_RE.match(captured)
                    if code:
                        scan.codes.setdefault(code.group(1), []).append(where)
                    else:
                        scan.uncoded.append((path.name, lineno, captured.strip()))
            for match in _ARG_CODE_RE.finditer(line):
                code = match.group(1) or match.group(2)
                scan.codes.setdefault(code, []).append(where)
    return scan


def load_review(path):
    """装载人工裁决台账（JSON：{cell_key: {status, note}}）。"""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('review ledger must be a JSON object')
    return data


def render_report(result):
    """人读报告：确定性、无时间戳；逐字节可复现。"""
    lines = ['CP state-space report (%d cells, %d actions)' % (
        len(result.cells), len({c.action for c in result.cells}))]
    for action, counts in result.summary().items():
        total = sum(counts.values())
        lines.append('  %-22s %4d  %s' % (
            action, total, ' '.join('%s=%d' % (d, counts[d])
                                    for d in sorted(counts))))
    if result.shadows:
        lines.append('shadowed cells:')
        lines.extend('  %s <- %s' % (c.key, ','.join(r.id for r in c.shadows))
                     for c in result.shadows)
    if result.undefined:
        lines.append('undefined cells (need disposition):')
        lines.extend('  %s' % c.key for c in result.undefined)
    if result.failures:
        lines.append('failures:')
        lines.extend('  %s' % f for f in result.failures)
    else:
        lines.append('OK: complete — every cell decided or dispositioned')
    return '\n'.join(lines) + '\n'
