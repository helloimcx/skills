#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_design.py — 原型目录（design/ / prototypes/）健康检查。

「原型先行」流程的确定性防线（skill: prototype-first，
spec: docs/specs/2026-10-08-proto-design-skill.md）。校验 README.md 清单表与
顶层 *.html 的双向一致性，只读、仅标准库、不写不删目标目录。

用法：
  python check_design.py <design_dir> [--quiet]

FAIL（任一存在 → exit 1）：
  1. README.md 缺失
  2. 清单表不存在（找不到含「文件」列的 markdown 表）
  3. 清单表缺「文件」或「状态」列
  4. 孤儿 html：盘上有、清单未登记
  5. 幽灵行：清单登记、盘上不存在
  6. 重复登记行：同一 html 出现多行
  7. 状态词汇非法（唯一合法前缀：待确认 / 已确认 / 已确认并实现）
  8. 清单行「文件」列解析不到 .html 文件名

WARN（不阻断，exit 0 仍打印）：
  1. 页面缺 `<!-- proto:` 头注释（前 40 行内）
  2. README 引用 assets 的 .css/.js 未带 `?v=` 版本 pin

--quiet：只输出 [FAIL]/[WARN] 行，抑制汇总与提示。
"""
import argparse
import re
import sys
from pathlib import Path

STATUS_VOCAB = ("已确认并实现", "已确认", "待确认")  # 最长前缀优先
FILE_COL = "文件"
STATUS_COL = "状态"
HTML_RE = re.compile(r"[\w.\-\\/]+\.(?:html)\b", re.IGNORECASE)  # \w 含 CJK，中文页名合法
SEP_RE = re.compile(r"^:?-{2,}:?$")
ASSETS_RE = re.compile(r"assets/[^ \t`()\'\"]+?\.(?:css|js)\b", re.IGNORECASE)
PROTO_MARK = "<!-- proto:"
HEAD_LINES = 40


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def split_cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def find_table(text: str):
    """返回清单表 (header_cells, row_lines)。

    优先取「文件+状态」双列皆备的表（README 前部可能有无关表格恰好带「文件」字样）；
    退而求其次取第一个含「文件」列的表（走「缺状态列」FAIL）；都没有返回 None。
    """
    lines = text.splitlines()
    tables = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        header = split_cells(lines[i])
        rows = []
        j = i + 1
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            rows.append(split_cells(lines[j]))
            j += 1
        if any(FILE_COL in c for c in header):
            tables.append((header, rows))
        i = j
    if not tables:
        return None
    for header, rows in tables:
        if any(STATUS_COL in c for c in header):
            return header, rows
    return tables[0]


def parse_rows(header, rows):
    """按列头定位「文件」「状态」列，跳过分隔行，返回 [(filename, status_cell)]。"""
    fi = next((i for i, c in enumerate(header) if FILE_COL in c), None)
    si = next((i for i, c in enumerate(header) if STATUS_COL in c), None)
    parsed = []
    for cells in rows:
        if all(SEP_RE.match(c.replace(" ", "")) or not c for c in cells):
            continue
        if fi is None or fi >= len(cells):
            continue
        m = HTML_RE.search(cells[fi])
        name = Path(m.group(0)).name if m else ""
        status = cells[si] if si is not None and si < len(cells) else ""
        parsed.append((name, status))
    return parsed


def valid_status(cell: str) -> bool:
    s = cell.strip().strip("*`_").strip()
    return any(s.startswith(v) for v in STATUS_VOCAB)


def scan_pages(design: Path):
    """WARN 扫描：顶层 html 缺 `<!-- proto:` 头注释。"""
    warns = []
    for p in sorted(design.glob("*.html")):
        head = "\n".join(read_text(p).splitlines()[:HEAD_LINES])
        if PROTO_MARK not in head:
            warns.append(f"[WARN] {p.name}: 前 {HEAD_LINES} 行缺 `{PROTO_MARK}` 头注释"
                         "（应写明对应需求 / 入口路径 / 数据契约）")
    return warns


def scan_assets_pin(readme_text: str):
    """WARN 扫描：README 引用 assets 的 css/js 未带 ?v= 版本 pin。"""
    warns, seen = [], set()
    for line in readme_text.splitlines():
        for m in ASSETS_RE.finditer(line):
            if "?v=" not in line and m.group(0) not in seen:
                seen.add(m.group(0))
                warns.append(f"[WARN] README 引用 `{m.group(0)}` 未带 `?v=` 版本 pin"
                             "（共享 assets 变更时用版本串刷缓存）")
    return warns


def check(design: Path):
    """返回 (fails, warns)。fails 非空 → exit 1。"""
    fails, warns = [], []
    readme = design / "README.md"
    if not readme.is_file():
        fails.append(f"[FAIL] 缺 {readme.name}——用 skill 模板 "
                     "references/template-README.md 初始化")
        return fails, warns
    text = read_text(readme)
    table = find_table(text)
    if table is None:
        fails.append("[FAIL] README.md 中找不到清单表（表头须含「文件」列）")
        return fails, warns
    header, rows = table
    if not any(STATUS_COL in c for c in header):
        fails.append(f"[FAIL] 清单表缺「{STATUS_COL}」列")
        return fails, warns

    entries = parse_rows(header, rows)
    disk = {p.name for p in design.glob("*.html")}
    counts = {}
    for name, status in entries:
        if not name:
            fails.append(f"[FAIL] 清单行「{FILE_COL}」列解析不到 .html 文件名")
            continue
        counts[name] = counts.get(name, 0) + 1
        if name not in disk:
            fails.append(f"[FAIL] 幽灵行: {name} 已登记但磁盘不存在")
        if not valid_status(status):
            fails.append(f"[FAIL] {name}: 状态词汇非法 {status.strip()!r}"
                         f"（合法前缀: {' / '.join(STATUS_VOCAB)}，可附括号备注）")
    for name, n in sorted(counts.items()):
        if n > 1:
            fails.append(f"[FAIL] {name} 重复登记 {n} 行（一个文件只允许一行）")
    for name in sorted(disk - {n for n, _ in entries}):
        fails.append(f"[FAIL] 孤儿原型: {name} 在磁盘上但清单未登记")
    warns.extend(scan_pages(design))
    warns.extend(scan_assets_pin(text))
    return fails, warns


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="原型目录清单一致性检查（只读）")
    ap.add_argument("design_dir", help="design/ 或 prototypes/ 目录")
    ap.add_argument("--quiet", action="store_true", help="只输出问题行，抑制汇总")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    design = Path(args.design_dir)
    if not design.is_dir():
        print(f"[FAIL] 目录不存在: {design}")
        return 1
    fails, warns = check(design)
    for line in fails + warns:
        print(line)
    if not args.quiet:
        if fails:
            print(f"\n检查未通过: {len(fails)} FAIL / {len(warns)} WARN → 清单与磁盘不一致，先修 README 数据行")
        else:
            print(f"\nOK: 清单与磁盘一致（{len(warns)} WARN，不阻断）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
