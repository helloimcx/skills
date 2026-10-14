#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_check_design.py — check_design.py 行为契约单测（自包含 unittest runner）。

锁定 docs/specs/2026-10-08-proto-design-skill.md 的 CLI 契约：

- FAIL（exit 1）：缺 README / 缺清单表 / 缺「文件」或「状态」列 / 孤儿 html（盘上有清单无）/
  幽灵行（清单有盘上无）/ 重复登记行 / 状态词汇非法。
- WARN（不阻断，exit 0）：页面缺 `<!-- proto:` 头注释 / README 引用 assets 未带 `?v=` pin。
- 状态词汇：待确认 / 已确认 / 已确认并实现（最长前缀优先），允许括号备注后缀。
- 健康目录 exit 0；--quiet 抑制非问题输出。

运行: python test_check_design.py
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "check_design.py"

PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<!-- proto:
需求: {title}（演示）
入口路径: /{slug}
数据契约: 无（纯展示）
-->
<h1>{title}</h1>
<script>var FAKE = [{{"q": "假数据"}}];</script>
</body>
</html>
"""

HEALTHY_README = """# design/ — 原型（唯一设计源）

## 文件清单

| 页面 | 文件 | 状态 | 对应实现 |
|---|---|---|---|
| 首页 | `index.html` | 已确认并实现（2026-10-01） | `/` |
| 报表 | `report.html` | 待确认 | 未开发 |

## 本地预览

<link rel="stylesheet" href="assets/app.css?v=20261008">
"""


def write_page(root: Path, name: str, title: str, with_comment: bool = True) -> None:
    html = PAGE.format(title=title, slug=Path(name).stem)
    if not with_comment:
        html = html.replace("<!-- proto:", "<!-- other:")
    (root / name).write_text(html, encoding="utf-8")


def run_check(design_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(design_dir), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


class CheckDesignTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "design"
        self.root.mkdir()

    def readme(self, text: str) -> None:
        (self.root / "README.md").write_text(text, encoding="utf-8")

    def healthy(self) -> None:
        self.readme(HEALTHY_README)
        write_page(self.root, "index.html", "首页")
        write_page(self.root, "report.html", "报表")

    # ---- 健康路径 ----

    def test_healthy_exit0(self):
        self.healthy()
        r = run_check(self.root)
        self.assertEqual(r.returncode, 0, f"stdout={r.stdout} stderr={r.stderr}")
        self.assertNotIn("[FAIL]", r.stdout)

    def test_status_parenthetical_suffix_ok(self):
        self.healthy()
        self.readme(HEALTHY_README.replace(
            "| 待确认 |", "| 待确认（2026-10-08 出稿，等用户走查） |"))
        r = run_check(self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_quiet_suppresses_non_issue_output(self):
        self.healthy()
        r = run_check(self.root, "--quiet")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    # ---- FAIL：缺文件 / 缺表 / 缺列 ----

    def test_missing_readme_fail(self):
        write_page(self.root, "index.html", "首页")
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("[FAIL]", r.stdout)
        self.assertIn("README.md", r.stdout)

    def test_readme_without_table_fail(self):
        self.readme("# design\n\n只有一段话，没有清单表。\n")
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("清单表", r.stdout)

    def test_table_missing_status_column_fail(self):
        self.readme("| 页面 | 文件 |\n|---|---|\n| 首页 | `index.html` |\n")
        write_page(self.root, "index.html", "首页")
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("状态", r.stdout)

    # ---- FAIL：双向一致 ----

    def test_orphan_html_fail(self):
        self.healthy()
        write_page(self.root, "rogue.html", "未登记页")
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("rogue.html", r.stdout)

    def test_ghost_row_fail(self):
        self.healthy()
        self.readme(HEALTHY_README.replace("| 报表 | `report.html`", "| 报表 | `gone.html`"))
        self.root.joinpath("report.html").unlink()
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("gone.html", r.stdout)

    def test_duplicate_row_fail(self):
        self.healthy()
        self.readme(HEALTHY_README.replace(
            "|---|---|---|---|\n",
            "|---|---|---|---|\n| 首页副本 | `index.html` | 已确认 | 同上 |\n"))
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("index.html", r.stdout)
        self.assertIn("重复", r.stdout)

    # ---- FAIL：状态词汇 ----

    def test_invalid_status_fail(self):
        self.healthy()
        self.readme(HEALTHY_README.replace("| 待确认 |", "| 原型已更新 |"))
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("状态", r.stdout)

    def test_empty_status_fail(self):
        self.healthy()
        self.readme(HEALTHY_README.replace("| 待确认 |", "|  |"))
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)

    def test_unparseable_file_cell_fail(self):
        self.healthy()
        self.readme(HEALTHY_README.replace("| 报表 | `report.html`", "| 报表 | （待补）"))
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn(".html", r.stdout)

    def test_chinese_page_name_ok(self):
        self.healthy()
        self.root.joinpath("report.html").unlink()
        write_page(self.root, "报表.html", "报表")
        self.readme(HEALTHY_README.replace(
            "| 报表 | `report.html` | 待确认 | 未开发 |",
            "| 报表 | `报表.html` | 待确认 | 未开发 |"))
        r = run_check(self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    # ---- 清单表识别 ----

    def test_prefers_manifest_table_over_earlier_table(self):
        self.healthy()
        self.readme("| 配置项 | 文件 |\n|---|---|\n| 主题 | `theme.json` |\n\n"
                    + HEALTHY_README)
        r = run_check(self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("[FAIL]", r.stdout)

    # ---- WARN：不阻断 ----

    def test_missing_proto_comment_warn(self):
        self.healthy()
        write_page(self.root, "report.html", "报表", with_comment=False)
        r = run_check(self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[WARN]", r.stdout)
        self.assertIn("report.html", r.stdout)

    def test_assets_without_version_pin_warn(self):
        self.healthy()
        self.readme(HEALTHY_README + "\n共享样式见 `assets/app.css`。\n")
        r = run_check(self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("[WARN]", r.stdout)

    def test_warn_does_not_fail_with_other_fails(self):
        self.healthy()
        write_page(self.root, "rogue.html", "未登记", with_comment=False)
        r = run_check(self.root)
        self.assertEqual(r.returncode, 1)
        self.assertIn("[WARN]", r.stdout)
        self.assertIn("[FAIL]", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
