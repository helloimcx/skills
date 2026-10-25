"""Keep reviewer instructions aligned with the live CP report contract."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / 'services' / 'code-loop'
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop.cp import gate


class ReviewerSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (ROOT / 'code-loop' / 'SKILL.md').read_text(encoding='utf-8')
        cls.reviewer = (ROOT / 'code-loop' / 'references' / 'reviewer.md').read_text(
            encoding='utf-8')

    def test_reviewer_decisions_match_control_plane(self):
        for decision in gate._EXPECTED_DECISIONS['reviewer']:
            self.assertIn(f'`{decision}`', self.reviewer)
        skill_reviewer_summary = next(
            line for line in self.skill.splitlines() if '`reviewer`：' in line
        )
        self.assertNotIn('LGTM', skill_reviewer_summary)
        self.assertNotIn('BLOCKED', skill_reviewer_summary)
        self.assertNotIn('FAILED', skill_reviewer_summary)

    def test_report_example_uses_only_live_fields_and_required_minimum(self):
        fields = set(gate._REPORT_FIELDS)
        example = next(
            block for block in self.reviewer.split('```json\n')[1:]
            if block.startswith('{')
        ).split('```', 1)[0]
        import json
        report = json.loads(example)
        self.assertLessEqual(set(report), fields)
        self.assertTrue({'decision', 'reviewer', 'summary'} <= set(report))
        self.assertIn('coverage', report)
        self.assertIn('action', report)
        self.assertNotIn('verdict', report)
        self.assertNotIn('checks', report)
        self.assertNotIn('feedback', report)

    def test_approval_requires_coverage_and_manual_merge(self):
        self.assertIn('coverage', self.reviewer)
        self.assertIn('必须包含 coverage', self.reviewer)
        self.assertIn('手工合并', self.reviewer)
        self.assertIn('不授权 Agent 自动合并', self.reviewer)
        self.assertIn('review_comment', self.reviewer)


if __name__ == '__main__':
    unittest.main()
