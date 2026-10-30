"""Keep reviewer instructions aligned with the live CP report contract."""
from pathlib import Path
import json
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
        examples = self._report_examples()
        self.assertEqual(
            {example['decision'] for example in examples},
            set(gate._EXPECTED_DECISIONS['reviewer']) - {'Blocked', 'Changes requested'},
        )
        for report in examples:
            self.assertLessEqual(set(report), gate._REPORT_FIELDS)
            self.assertTrue({'decision', 'reviewer', 'summary'} <= set(report))
            self.assertNotIn('verdict', report)
            self.assertNotIn('checks', report)
            self.assertNotIn('feedback', report)

    def _report_examples(self):
        return [json.loads(block.split('```', 1)[0])
                for block in self.reviewer.split('```json\n')[1:]
                if block.startswith('{')]

    def test_approved_and_failed_examples_match_runtime_report_and_action_contracts(self):
        examples = self._report_examples()
        self.assertEqual(len(examples), 2)
        item = {'kind': 'reviewer'}
        payload = {'pr': {'number': 123, 'head': {
            'sha': 'example-head', 'ref': 'feature/example'}}}
        execution = {'id': 'example-execution', 'worker': 'handler-agent'}
        service = type('Service', (), {
            'platform': type('Platform', (), {'validate_action': lambda *_: None})(),
            'store': type('Store', (), {
                'evidence': lambda *_: [],
                'db': type('DB', (), {'execute': lambda *_: type(
                    'Cursor', (), {'fetchall': lambda *_: []})()})(),
            })(),
        })()

        for report in examples:
            with self.subTest(decision=report['decision']):
                self.assertIsNone(gate.validate_report(
                    service, execution, item, payload, 'example-execution', report))
                gate.authorize(service, item, payload, report)

    def test_legacy_report_fields_and_decisions_are_rejected_by_runtime(self):
        service = type('Service', (), {
            'platform': type('Platform', (), {'validate_action': lambda *_: None})(),
            'store': type('Store', (), {
                'evidence': lambda *_: [],
                'db': type('DB', (), {'execute': lambda *_: type(
                    'Cursor', (), {'fetchall': lambda *_: []})()})(),
            })(),
        })()
        execution = {'id': 'example-execution', 'worker': 'handler-agent'}
        item = {'kind': 'reviewer'}

        for invalid in (
                {'decision': 'LGTM', 'reviewer': 'reviewer-agent', 'summary': 'old'},
                {'decision': 'Changes requested', 'reviewer': 'reviewer-agent',
                 'summary': 'old', 'verdict': 'Changes requested'}):
            with self.subTest(report=invalid), self.assertRaises(ValueError):
                gate.validate_report(
                    service, execution, item, {}, 'example-execution', invalid)

    def test_approval_requires_coverage_and_manual_merge(self):
        self.assertIn('coverage', self.reviewer)
        self.assertIn('必须包含 coverage', self.reviewer)
        self.assertIn('手工合并', self.reviewer)
        self.assertIn('不授权 Agent 自动合并', self.reviewer)
        self.assertIn('review_comment', self.reviewer)


if __name__ == '__main__':
    unittest.main()
