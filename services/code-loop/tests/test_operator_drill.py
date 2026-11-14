import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeloop.operator.drill import run_drill


class OperatorDrillTests(unittest.TestCase):
    def test_builtin_drill_uses_gate_and_never_executes_actions(self):
        result = run_drill()
        self.assertTrue(result['passed'])
        self.assertEqual(result['mode'], 'isolated-dry-run')
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['production_actions'], 0)
        self.assertEqual([s['scenario'] for s in result['scenarios']], [
            'worker-disconnected', 'collection-stale', 'model-unavailable'])
        self.assertEqual([s['assertions']['expected_decision']
                          for s in result['scenarios']],
                         ['fired', 'fired', 'skipped_rule'])


if __name__ == '__main__':
    unittest.main()
