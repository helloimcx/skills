"""dashboard.py register CLI tests: project trigger registration UX
(derive-once defaults, edit semantics, fail-closed validation)."""
import json
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import store  # noqa: E402
from dashboard import register_trigger  # noqa: E402


class RegisterTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / 'proj'
        (self.repo / '.agents' / 'loops' / 'reviewer').mkdir(parents=True)
        (self.repo / '.agents' / 'loops' / 'reviewer' / 'prompt.md').write_text(
            'entry', encoding='utf-8')
        (self.repo / 'skills' / 'code-loop' / 'references').mkdir(parents=True)
        (self.repo / 'skills' / 'code-loop' / 'references' / 'reviewer.md').write_text(
            'rules', encoding='utf-8')
        self.db = store.connect(root / 'dash.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def row(self, org='a__b'):
        return self.db.execute('SELECT * FROM projects WHERE org_repo=?',
                               (org,)).fetchone()

    def test_first_registration_derives_defaults(self):
        code, _ = register_trigger(self.db, 'a__b', repo=str(self.repo))
        self.assertEqual(0, code)
        row = self.row()
        self.assertEqual(str(self.repo), row['repo_cwd'])
        self.assertEqual(str(self.repo / '.agents' / 'loops' / 'reviewer' / 'prompt.md'),
                         row['entry_doc'])
        self.assertEqual(str(self.repo / 'skills' / 'code-loop' / 'references'
                             / 'reviewer.md'), row['rules_doc'])
        self.assertEqual(['claude', '-p', '--dangerously-skip-permissions'],
                         json.loads(row['spawn_command']))

    def test_first_registration_requires_repo(self):
        code, message = register_trigger(self.db, 'a__b')
        self.assertEqual(1, code)
        self.assertIn('REPO_REQUIRED', message)
        self.assertIsNone(self.row())

    def test_missing_conventional_entry_doc_fails_closed(self):
        (self.repo / '.agents' / 'loops' / 'reviewer' / 'prompt.md').unlink()
        code, message = register_trigger(self.db, 'a__b', repo=str(self.repo))
        self.assertEqual(1, code)
        self.assertIn('ENTRY_DOC_MISSING', message)

    def test_explicit_docs_override_and_must_exist(self):
        missing = self.repo / 'custom-entry.md'
        code, message = register_trigger(self.db, 'a__b', repo=str(self.repo),
                                         entry_doc=str(missing))
        self.assertEqual(1, code)
        self.assertIn('ENTRY_DOC_NOT_FOUND', message)
        rules = self.repo / 'other-rules.md'
        rules.write_text('r', encoding='utf-8')
        code, _ = register_trigger(self.db, 'a__b', repo=str(self.repo),
                                   entry_doc=str(rules), rules_doc=str(rules))
        self.assertEqual(0, code)
        self.assertEqual(str(rules), self.row()['entry_doc'])
        self.assertEqual(str(rules), self.row()['rules_doc'])

    def test_edit_updates_only_provided_keys(self):
        register_trigger(self.db, 'a__b', repo=str(self.repo))
        other = self.repo / 'new-rules.md'
        other.write_text('n', encoding='utf-8')
        code, _ = register_trigger(self.db, 'a__b', rules_doc=str(other))
        self.assertEqual(0, code)
        row = self.row()
        self.assertEqual(str(other), row['rules_doc'])
        self.assertEqual(str(self.repo), row['repo_cwd'])
        self.assertEqual(['claude', '-p', '--dangerously-skip-permissions'],
                         json.loads(row['spawn_command']))

    def test_command_parsed_from_comma_list(self):
        code, _ = register_trigger(self.db, 'a__b', repo=str(self.repo),
                                   command='C:/x/claude.exe,-p')
        self.assertEqual(0, code)
        self.assertEqual(['C:/x/claude.exe', '-p'],
                         json.loads(self.row()['spawn_command']))

    def test_repo_must_exist(self):
        code, message = register_trigger(self.db, 'a__b', repo='D:/nope')
        self.assertEqual(1, code)
        self.assertIn('REPO_NOT_FOUND', message)

    def test_listing_reports_registrations(self):
        register_trigger(self.db, 'a__b', repo=str(self.repo))
        code, message = register_trigger(self.db, '', listing=True)
        self.assertEqual(0, code)
        self.assertIn('a__b', message)

    def test_coordinator_runtime_can_be_registered_per_project(self):
        code, _ = register_trigger(
            self.db, 'a__b', repo=str(self.repo),
            coordinator_agent='opencode', coordinator_model='provider/model')
        self.assertEqual(0, code)
        row = self.row()
        self.assertEqual(('opencode', 'provider/model'),
                         (row['coordinator_agent'], row['coordinator_model']))
        code, message = register_trigger(
            self.db, 'a__b', coordinator_agent='pi')
        self.assertEqual(1, code)
        self.assertIn('SUBAGENT_UNSUPPORTED', message)


if __name__ == '__main__':
    unittest.main()
