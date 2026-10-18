"""check_ledger tests (issue #94): snapshot table must keep one issue per row.

AC3: a ledger containing a ×N batch row is detected by the validation.
"""
import io
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import check_ledger

CLEAN = """# handler 处置台账

## 快照表（2026-09-11，全量 Open Issues）

| # | 标题 | 状态 | 理由 |
|---|------|------|------|
| 94 | [Content] handler 回流确定化 | SKIP | 理由 |
| 76 | [BUG] gitea create 重复 PR | SKIP | 理由 |

## 轮次日志

- 2026-09-11 / 本轮任务=0
"""

BATCH = """\
# handler 处置台账

## 快照表（2026-09-11，全量 Open Issues）

| # | 标题 | 状态 | 理由 |
|---|------|------|------|
| 225/153/…/7 决策类 ×12 | 批量行 | WAIT_HUMAN | 静态理由跨轮照抄 |
| 94 | 独立行 | SKIP | 正常 |

## 轮次日志
"""


class CheckLedgerTests(unittest.TestCase):
    def test_clean_ledger_passes(self):
        violations, checked = check_ledger.check(CLEAN)
        self.assertEqual([], violations)
        self.assertEqual(2, checked)

    def test_batch_row_detected(self):
        """AC3：×N 批量行被检出，且正常行不误报。"""
        violations, checked = check_ledger.check(BATCH)
        self.assertEqual(1, len(violations))
        lineno, first, raw = violations[0]
        self.assertIn('×12', first)
        self.assertIn('225/153', first)
        self.assertIsInstance(lineno, int)
        self.assertEqual(2, checked)  # 批量行 + 独立行都计入检查

    def test_missing_snapshot_table_reports_none(self):
        violations, _ = check_ledger.check('# 无快照表的台账\n\n- 只有日志\n')
        self.assertIsNone(violations)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')
            batch = Path(tmp) / 'batch.md'
            batch.write_text(BATCH, encoding='utf-8')

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = check_ledger.main([str(clean)])
            self.assertEqual(0, rc)
            self.assertIn('PASS', out.getvalue())

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = check_ledger.main([str(batch)])
            self.assertEqual(1, rc)
            self.assertIn('FAIL', err.getvalue())
            self.assertIn('×12', err.getvalue())

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = check_ledger.main([str(Path(tmp) / 'missing.md')])
            self.assertEqual(2, rc)

    def test_expect_matching_count_passes(self):
        """--expect 与快照表行数一致 → PASS（issue #355 数量校验）。"""
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')  # 2 行
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = check_ledger.main([str(clean), '--expect', '2'])
            self.assertEqual(0, rc)
            self.assertIn('PASS', out.getvalue())
            self.assertIn('与平台 open 数一致', out.getvalue())

    def test_expect_mismatch_fails(self):
        """issue #355：快照表行数 ≠ 平台 open 数 → FAIL（已合并 PR 的 issue 未移除）。"""
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')  # 2 行
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = check_ledger.main([str(clean), '--expect', '5'])
            self.assertEqual(1, rc)
            self.assertIn('数量校验 FAIL', err.getvalue())
            self.assertIn('2 行', err.getvalue())
            self.assertIn('5 个', err.getvalue())

    def test_gitea_flag_counts_platform(self):
        """--gitea 自动统计平台 open 数并与快照表行数比对。"""
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')  # 2 行
            fake_out = "#355   open   mocx   title\n#351   open   mocx   title2\n"
            with unittest.mock.patch('check_ledger._gitea_cmd', return_value=['gitea']), \
                 unittest.mock.patch('subprocess.run') as mock_run:
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout=fake_out, stderr='')
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = check_ledger.main([str(clean), '--gitea'])
            self.assertEqual(0, rc)  # 平台 2 个 == 快照 2 行
            self.assertIn('PASS', out.getvalue())
            mock_run.assert_called_once()

    def test_gitea_flag_mismatch_fails(self):
        """--gitea 平台 open 数与快照行数不一致 → FAIL。"""
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')  # 2 行
            fake_out = "#355   open   moc   title\n"
            with unittest.mock.patch('check_ledger._gitea_cmd', return_value=['gitea']), \
                 unittest.mock.patch('subprocess.run') as mock_run:
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout=fake_out, stderr='')
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = check_ledger.main([str(clean), '--gitea'])
            self.assertEqual(1, rc)  # 平台 1 个 != 快照 2 行
            self.assertIn('数量校验 FAIL', err.getvalue())

    def test_github_flag_counts_platform(self):
        """--github 自动统计平台 open 数并与快照表行数比对。"""
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')  # 2 行
            fake_out = "#355\topen\tmocx\ttitle\n#351\topen\tmocx\ttitle2\n"
            with unittest.mock.patch('check_ledger._github_cmd', return_value=['gh']), \
                 unittest.mock.patch('subprocess.run') as mock_run:
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout=fake_out, stderr='')
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = check_ledger.main([str(clean), '--github'])
            self.assertEqual(0, rc)  # 平台 2 个 == 快照 2 行
            self.assertIn('PASS', out.getvalue())
            mock_run.assert_called_once()

    def test_github_flag_mismatch_fails(self):
        """--github 平台 open 数与快照行数不一致 → FAIL。"""
        with tempfile.TemporaryDirectory() as tmp:
            clean = Path(tmp) / 'clean.md'
            clean.write_text(CLEAN, encoding='utf-8')  # 2 行
            fake_out = "#355\topen\tmoc\ttitle\n"
            with unittest.mock.patch('check_ledger._github_cmd', return_value=['gh']), \
                 unittest.mock.patch('subprocess.run') as mock_run:
                mock_run.return_value = unittest.mock.Mock(
                    returncode=0, stdout=fake_out, stderr='')
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = check_ledger.main([str(clean), '--github'])
            self.assertEqual(1, rc)  # 平台 1 个 != 快照 2 行
            self.assertIn('数量校验 FAIL', err.getvalue())


if __name__ == '__main__':
    unittest.main(verbosity=2)
