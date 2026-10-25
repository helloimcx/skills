"""Event trigger layer: deterministic rules, guards, doorbell purity, spawn."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop import trigger

NOW = 1_700_000_000.0
TODAY = trigger.day_key(NOW)


def plain_issue(n, title, iid=None):
    return {"id": iid if iid is not None else 1000 + n, "number": n,
            "state": "open", "title": title, "body": ""}


class FakeGitea:
    def __init__(self, issues):
        self.issues = issues
        self.calls = 0

    def list_issues(self, state='open'):
        self.calls += 1
        if isinstance(self.issues, Exception):
            raise self.issues
        return self.issues


class FakeService:
    def __init__(self, enabled=True, running=0):
        self.enabled = enabled
        self.running = running

    def status(self):
        return {"enabled": self.enabled, "executions": {"RUNNING": self.running}}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='trigger-test-'))
        (self.tmp / 'repo').mkdir()
        (self.tmp / 'prompt.md').write_text('entry doc', encoding='utf-8')
        (self.tmp / 'handler.md').write_text('rules doc', encoding='utf-8')
        self.state_dir = self.tmp / 'state'
        self.gitea = FakeGitea([])
        self.service = FakeService()
        self.write_state()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def clear_state(self):
        (self.state_dir / 'trigger-state.json').unlink(missing_ok=True)

    def cfg(self, **over):
        cfg = {
            "loop": "handler",
            "repo_cwd": str(self.tmp / 'repo'),
            "entry_doc": str(self.tmp / 'prompt.md'),
            "rules_doc": str(self.tmp / 'handler.md'),
            "rules": [{"title_prefix_any": ["[BUG]"]}],
        }
        cfg.update(over)
        path = self.tmp / 'triggers.json'
        path.write_text(json.dumps(cfg), encoding='utf-8')
        return path

    def provider(self, path, cwd):
        return {"state_dir": str(self.state_dir), "platform": {}}

    def write_state(self, **over):
        state = {"seen": {}, "last_fired": 0.0, "fired_date": TODAY, "fired_count": 0}
        state.update(over)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / 'trigger-state.json').write_text(
            json.dumps(state), encoding='utf-8')
        return state

    def read_state(self):
        return json.loads((self.state_dir / 'trigger-state.json').read_text(encoding='utf-8'))

    def rewind_cooldown(self):
        state = self.read_state()
        state['last_fired'] = 0.0
        (self.state_dir / 'trigger-state.json').write_text(
            json.dumps(state), encoding='utf-8')

    def fire(self, mode='run', dry_run=True, fail_if_called=False, cfg_over=None,
            popen_fn=None, which_fn=None):
        if fail_if_called:
            class Boom:
                def list_issues(self, state='open'):
                    raise AssertionError('network reached')
            self.gitea = Boom()
        return trigger.run(self.cfg(**(cfg_over or {})), mode=mode, dry_run=dry_run,
                           gitea_factory=lambda p: self.gitea,
                           service_factory=lambda c: self.service,
                           config_provider=self.provider, now=NOW,
                           popen_fn=popen_fn, which_fn=which_fn)


class ConfigTests(Harness):
    def test_defaults_applied(self):
        cfg = trigger.load_triggers(self.cfg())
        self.assertEqual(30, cfg['cooldown_minutes'])
        self.assertEqual(12, cfg['max_rounds_per_day'])
        self.assertEqual(["claude", "-p", "--dangerously-skip-permissions"],
                         cfg['spawn']['command'])
        self.assertIn('{rules_doc}', cfg['spawn']['prompt_template'])
        self.assertIn('{entry_doc}', cfg['spawn']['prompt_template'])

    def test_unknown_top_level_key_rejected(self):
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg(webhook='http://x'))

    def test_invalid_loop_or_missing_rules_rejected(self):
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg(loop='worker'))
        path = self.tmp / 't2.json'
        path.write_text(json.dumps({"loop": "handler", "repo_cwd": str(self.tmp),
                                    "entry_doc": 'e', "rules_doc": 'r'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            trigger.load_triggers(path)

    def test_rule_schema_strict(self):
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg(rules=[{"labels_any": ["bug"]}]))
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg(rules=[{"title_prefix_any": []}]))
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg(rules=[{"title_prefix_any": ["[BUG]"], "junk": 1}]))

    def test_missing_doc_files_rejected(self):
        (self.tmp / 'prompt.md').unlink()
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg())
        (self.tmp / 'prompt.md').write_text('entry doc', encoding='utf-8')
        (self.tmp / 'handler.md').unlink()
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg())

    def test_template_placeholders_required(self):
        with self.assertRaises(ValueError):
            trigger.load_triggers(self.cfg(spawn={
                "prompt_template": "no placeholders here"}))
        cfg = trigger.load_triggers(self.cfg(spawn={
            "prompt_template": "Read {rules_doc} with {entry_doc}"}))
        self.assertEqual('Read {rules_doc} with {entry_doc}',
                         cfg['spawn']['prompt_template'])


class MatchAndStateTests(Harness):
    def test_first_seen_match_and_persisted_seen(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] gate broken')])
        decision = self.fire()
        self.assertEqual('SPAWNED_DRYRUN', decision['event'])
        self.assertEqual([7], decision['matched_numbers'])
        self.assertIn('1007', self.read_state()['seen'])

    def test_second_run_no_rematch(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] gate broken')])
        self.fire()
        self.rewind_cooldown()
        self.assertEqual('SKIPPED_NO_MATCH', self.fire()['event'])

    def test_bootstrap_primes_without_spawn(self):
        self.clear_state()
        self.gitea = FakeGitea([plain_issue(7, '[BUG] gate broken')])
        decision = self.fire()
        self.assertEqual('SKIPPED_BOOTSTRAP', decision['event'])
        self.assertEqual([7], decision['matched_numbers'])
        self.assertIn('1007', self.read_state()['seen'])
        self.assertEqual('SKIPPED_NO_MATCH', self.fire()['event'])

    def test_non_matching_title_ignored(self):
        self.gitea = FakeGitea([plain_issue(8, 'fix stuff please')])
        decision = self.fire()
        self.assertEqual('SKIPPED_NO_MATCH', decision['event'])
        self.assertEqual({}, self.read_state()['seen'])

    def test_closed_pruned_and_reopen_rematches(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.fire()
        self.rewind_cooldown()
        self.gitea = FakeGitea([])
        self.fire()
        self.assertEqual({}, self.read_state()['seen'])
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.assertEqual('SPAWNED_DRYRUN', self.fire()['event'])

    def test_multiple_matches_coalesce_into_one_wake(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] a'), plain_issue(9, '[BUG] b')])
        decision = self.fire()
        self.assertEqual([7, 9], decision['matched_numbers'])
        self.assertEqual('SPAWNED_DRYRUN', decision['event'])


class GuardTests(Harness):
    def test_cooldown_blocks_before_network(self):
        self.write_state(last_fired=NOW - 60)
        decision = self.fire(fail_if_called=True)
        self.assertEqual('SKIPPED_COOLDOWN', decision['event'])

    def test_not_enabled_blocks_before_network(self):
        self.service = FakeService(enabled=False)
        decision = self.fire(fail_if_called=True)
        self.assertEqual('SKIPPED_NOT_ENABLED', decision['event'])

    def test_busy_skips_and_keeps_seen_clean_for_retry(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.service = FakeService(running=1)
        decision = self.fire()
        self.assertEqual('SKIPPED_BUSY', decision['event'])
        self.assertEqual([7], decision['matched_numbers'])
        self.assertEqual({}, self.read_state()['seen'])
        self.service = FakeService()
        self.assertEqual('SPAWNED_DRYRUN', self.fire()['event'])

    def test_daily_cap_after_match(self):
        self.write_state(fired_count=12)
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        decision = self.fire()
        self.assertEqual('SKIPPED_DAILY_CAP', decision['event'])

    def test_busy_reported_before_daily_cap(self):
        self.write_state(fired_count=12)
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.service = FakeService(running=1)
        self.assertEqual('SKIPPED_BUSY', self.fire()['event'])

    def test_daily_counter_resets_next_day(self):
        self.write_state(fired_count=12, fired_date='2000-01-01')
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.assertEqual('SPAWNED_DRYRUN', self.fire()['event'])

    def test_api_failure_is_error_and_cursor_untouched(self):
        self.gitea = FakeGitea(RuntimeError('boom'))
        decision = self.fire()
        self.assertEqual('ERROR', decision['event'])
        self.assertFalse(decision['ok'])
        self.assertEqual({}, self.read_state()['seen'])


class PurityTests(Harness):
    def test_prompt_carries_no_event_payload(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] secret-token-xyz leaked')])
        decision = self.fire()
        prompt = decision['prompt']
        self.assertNotIn('secret-token-xyz', prompt)
        self.assertNotIn('[BUG]', prompt)
        self.assertNotIn('leaked', prompt)
        self.assertIn(str(self.tmp / 'handler.md'), prompt)
        self.assertIn(str(self.tmp / 'prompt.md'), prompt)

    def test_custom_template_substitution(self):
        over = {"spawn": {"prompt_template": "RULES={rules_doc}; ENTRY={entry_doc}"}}
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        decision = self.fire(cfg_over=over)
        self.assertIn('RULES=%s; ENTRY=%s' % (self.tmp / 'handler.md', self.tmp / 'prompt.md'),
                      decision['prompt'])


class BuildRoundPromptTests(unittest.TestCase):
    """build_round_prompt is the one manual-round prompt constructor. Each
    loop gets its own rule/entry docs: the registration's docs belong to the
    reviewer loop, so reviewer resolves them directly while handler/discover
    resolve sibling docs and repo-convention entry paths (2026-09-24: a
    handler repair session opened by reading reviewer.md and looked like a
    reviewer round). Reviewer output stays byte-identical to the pre-registry
    composition; the cron template itself is untouched."""

    TMP = Path(tempfile.gettempdir())

    def setUp(self):
        # a private mkdtemp per test: fixed shared paths under gettempdir
        # would let a parallel run rmtree another run's fixtures mid-flight
        self.tmp = Path(tempfile.mkdtemp(prefix='round-prompt-'))
        self.skill = self.tmp / 'skill'
        self.rules = str(self.skill / 'references' / 'reviewer.md')
        self.handler_doc = self.skill / 'references' / 'handler.md'
        self.discover_doc = self.skill / 'references' / 'discover.md'
        self.repo = self.tmp / 'repo'
        self.entry = str(self.repo / '.agents' / 'loops' / 'reviewer' / 'prompt.md')
        self.handler_entry = self.repo / '.agents' / 'loops' / 'handler' / 'prompt.md'
        self.discover_entry = self.repo / '.agents' / 'loops' / 'discover' / 'prompt.md'
        self.row = {'rules_doc': self.rules, 'entry_doc': self.entry,
                    'repo_cwd': str(self.repo)}
        self.claim = {'execution': 'e' * 32, 'token': 9,
                      'worktree': str(self.tmp / 'repair-pr-7'),
                      'branch': 'fix/mocx/repair-pr-7',
                      'head_ref': 'feature/mocx/x'}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, text='loop doc'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    # -- reviewer: registered docs, byte-identical to the old composition ---

    def test_reviewer_targeted_matches_template_composition(self):
        out = trigger.build_round_prompt('reviewer', self.row, 7, None)
        self.assertEqual(out, (trigger.DEFAULT_PROMPT_TEMPLATE
                               + trigger.TARGETED_PROMPT_SUFFIX).format(
            rules_doc=self.rules, entry_doc=self.entry, number=7))

    def test_reviewer_prepared_matches_template_composition(self):
        out = trigger.build_round_prompt('reviewer', self.row, 7, self.claim)
        self.assertEqual(out, (trigger.DEFAULT_PROMPT_TEMPLATE
                               + trigger.PREPARED_TARGETED_PROMPT_SUFFIX).format(
            rules_doc=self.rules, entry_doc=self.entry, number=7,
            execution=self.claim['execution'], token=self.claim['token'],
            worktree=self.claim['worktree']))

    def test_reviewer_unscoped_is_the_cron_template(self):
        out = trigger.build_round_prompt('reviewer', self.row, None, None)
        self.assertEqual(out, trigger.DEFAULT_PROMPT_TEMPLATE.format(
            rules_doc=self.rules, entry_doc=self.entry))

    def test_targeted_directive_carries_the_fallback_route(self):
        out = trigger.build_round_prompt('reviewer', self.row, 7, None)
        self.assertIn('claim --loop reviewer --item review:7', out)
        # The pointer must name the reviewer.md section verbatim.
        self.assertIn('《定向评审（看板手动轮）》', out)

    def test_prepared_targeted_carries_the_prepared_round(self):
        """The dashboard pre-claims, prepares and binds the round; the prompt
        hands the session that fact — machine-local CP runtime values only, and
        the classic claim route stays reachable as the fallback."""
        out = trigger.build_round_prompt('reviewer', self.row, 7, self.claim)
        self.assertTrue(out.startswith(trigger.DEFAULT_PROMPT_TEMPLATE.format(
            rules_doc=self.rules, entry_doc=self.entry)))
        self.assertIn('execution=%s' % ('e' * 32), out)
        self.assertIn('token=9', out)
        self.assertIn('worktree=%s' % self.claim['worktree'], out)
        self.assertIn('不要 claim、不要 bind、不要另建 worktree', out)
        self.assertIn('claim --loop reviewer --item review:7', out)   # fallback route

    def test_rejects_non_int_or_non_positive_targets(self):
        for bad in (0, -1, '7', True, False, 7.0):
            with self.assertRaises(ValueError):
                trigger.build_round_prompt('reviewer', self.row, bad, None)

    def test_prepared_round_without_target_is_a_loud_error(self):
        with self.assertRaises(ValueError) as caught:
            trigger.build_round_prompt('reviewer', self.row, None, self.claim)
        self.assertIn('ROUND_CLAIM_WITHOUT_TARGET', str(caught.exception))

    def test_round_claim_fields_are_validated(self):
        good = str(Path(tempfile.gettempdir()) / 'w')
        for bad in ({'execution': '', 'token': 1, 'worktree': good},
                    {'execution': 'x', 'token': 0, 'worktree': good},
                    {'execution': 'x', 'token': True, 'worktree': good},
                    {'execution': 'x', 'token': '1', 'worktree': good},
                    {'execution': 'x', 'token': 1, 'worktree': 'relative/w'},
                    {'execution': 'x', 'token': 1},
                    'not-a-mapping', [], ('execution',)):
            with self.assertRaises(ValueError):
                trigger.build_round_prompt('reviewer', self.row, 7, round_claim=bad)

    def test_unknown_loop_is_loud(self):
        with self.assertRaises(ValueError) as caught:
            trigger.build_round_prompt('nope', self.row, 7, None)
        self.assertIn('UNKNOWN_ROUND_LOOP', str(caught.exception))

    # -- handler: sibling rule doc, convention entry doc, handler-native -----

    def test_handler_prepared_is_handler_native(self):
        """The 2026-09-24 defect: a repair session opened by reading the
        registered reviewer docs and looked like a reviewer round. The handler
        prompt must name the handler doc as the rules source and never
        reference the reviewer registration — no override-suppression left."""
        self._write(self.handler_doc)
        out = trigger.build_round_prompt('handler', self.row, 7, self.claim)
        self.assertIn(str(self.handler_doc), out)
        self.assertIn('规则唯一源', out)
        self.assertNotIn(self.rules, out)
        self.assertNotIn(self.entry, out)
        self.assertNotIn('reviewer', out)
        self.assertNotIn('优先于入口/规则文档', out)
        # the prepared handoff and the scoped task
        self.assertIn('repair:7', out)
        self.assertIn('execution=%s' % ('e' * 32), out)
        self.assertIn('token=9', out)
        self.assertIn('worktree=%s' % self.claim['worktree'], out)
        self.assertIn('分支 %s，已 bind' % self.claim['branch'], out)
        self.assertIn('不要 claim、不要 bind、不要另建 worktree', out)
        self.assertIn('《PR 冲突解决（兜底）》', out)
        self.assertIn('claim --loop handler --item repair:7', out)   # fallback
        # the resolution is committed locally; the push is publish's job
        self.assertIn('push 由 publish 完成', out)
        self.assertIn('不要先手动 push 再 publish', out)

    def test_handler_states_the_closing_contract(self):
        """Spec 2026-09-22-repair-round-speedup: the four closing facts are
        spelled out so the session never re-derives the publish protocol
        (the 03d8c5c7 postmortem: 2 minutes of protocol archaeology, then a
        silent death before report/publish)."""
        self._write(self.handler_doc)
        out = trigger.build_round_prompt('handler', self.row, 7, self.claim)
        self.assertIn('action: "edit_pr"', out)
        self.assertIn('{number, title, body}', out)
        self.assertIn('INVALID_PUBLICATION_PAYLOAD', out)
        self.assertIn('非 force 推回原 PR 分支 feature/mocx/x', out)
        self.assertIn('STALE_INPUT', out)
        self.assertIn('自动置 CANCELLED', out)

    def test_handler_prompt_carries_review_and_conflict_task_reasons(self):
        self._write(self.handler_doc)
        review = trigger.build_round_prompt(
            'handler', self.row, 7,
            dict(self.claim, task='PR_REVIEW_FIX',
                 repair_reasons=['changes_requested']))
        self.assertIn('task=PR_REVIEW_FIX', review)
        self.assertIn('repair_reasons=changes_requested', review)
        self.assertIn('只处理评审指出的问题', review)
        self.assertNotIn('解决冲突', review)

        combined = trigger.build_round_prompt(
            'handler', self.row, 7,
            dict(self.claim, task='PR_REVIEW_FIX',
                 repair_reasons=['changes_requested', 'merge_conflict']))
        self.assertIn('先按评审意见修复，再合并', combined)

    def test_handler_entry_doc_is_a_soft_convention(self):
        self._write(self.handler_doc)
        self._write(self.handler_entry)
        with_entry = trigger.build_round_prompt('handler', self.row, 7, self.claim)
        self.assertIn(str(self.handler_entry), with_entry)
        self.handler_entry.unlink()
        without_entry = trigger.build_round_prompt(
            'handler', self.row, 7, self.claim)
        self.assertNotIn(str(self.handler_entry), without_entry)

    def test_handler_requires_the_prepared_handoff(self):
        self._write(self.handler_doc)
        with self.assertRaises(ValueError) as caught:
            trigger.build_round_prompt('handler', self.row, 7, None)
        self.assertIn('ROUND_CLAIM_REQUIRED', str(caught.exception))
        for field in ('branch', 'head_ref'):
            with self.assertRaises(ValueError) as caught:
                trigger.build_round_prompt(
                    'handler', self.row, 7, dict(self.claim, **{field: None}))
            self.assertIn('ROUND_CLAIM_INVALID', str(caught.exception))
            claim = dict(self.claim)
            del claim[field]
            with self.assertRaises(ValueError):
                trigger.build_round_prompt('handler', self.row, 7, claim)

    def test_missing_handler_doc_is_a_loud_error(self):
        for bad_rules in (self.rules,
                          str(self.skill / 'nowhere' / 'reviewer.md')):
            with self.assertRaises(ValueError) as caught:
                trigger.build_round_prompt(
                    'handler', dict(self.row, rules_doc=bad_rules), 7, self.claim)
            self.assertIn('HANDLER_DOC_MISSING', str(caught.exception))

    # -- discover: an unscoped manual round is the cron template -------------

    def test_discover_unscoped_is_the_cron_template(self):
        self._write(self.discover_doc)
        self._write(self.discover_entry)
        out = trigger.build_round_prompt('discover', self.row, None, None)
        self.assertEqual(out, trigger.DEFAULT_PROMPT_TEMPLATE.format(
            rules_doc=str(self.discover_doc), entry_doc=str(self.discover_entry)))

    def test_discover_entry_doc_is_soft_and_targeting_is_refused(self):
        self._write(self.discover_doc)
        out = trigger.build_round_prompt('discover', self.row, None, None)
        self.assertEqual(out, trigger.DEFAULT_PROMPT_TEMPLATE_NO_ENTRY.format(
            rules_doc=str(self.discover_doc)))
        with self.assertRaises(ValueError):
            trigger.build_round_prompt('discover', self.row, None, self.claim)
        with self.assertRaises(ValueError):
            trigger.build_round_prompt('discover', self.row, 7, None)


class SpawnTests(Harness):
    def test_dry_run_never_spawns_process(self):
        def popen(*a, **k):
            raise AssertionError('process spawned in dry-run')
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        decision = self.fire(dry_run=True, popen_fn=popen)
        self.assertEqual('SPAWNED_DRYRUN', decision['event'])

    def test_check_mode_reports_matched_without_spawn(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        decision = self.fire(mode='check')
        self.assertEqual('MATCHED', decision['event'])

    def test_cmd_bridge_and_stdin_prompt(self):
        import os
        captured = {}

        def popen(argv, **kwargs):
            captured['argv'] = list(argv)
            captured['cwd'] = kwargs.get('cwd')
            captured['stdin'] = kwargs.get('stdin').read()
            captured['flags'] = kwargs.get('creationflags')
            captured['session'] = kwargs.get('start_new_session')
            kwargs.get('stdin').close()
            return type('P', (), {'pid': 4321})

        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        decision = self.fire(dry_run=False, popen_fn=popen,
                             which_fn=lambda x: 'C:/fake/claude.cmd')
        self.assertEqual('SPAWNED', decision['event'])
        self.assertEqual(4321, decision['pid'])
        self.assertEqual(['cmd.exe', '/c', 'C:/fake/claude.cmd',
                          '-p', '--dangerously-skip-permissions'], captured['argv'])
        self.assertEqual(str(self.tmp / 'repo'), captured['cwd'])
        self.assertEqual(decision['prompt'], captured['stdin'].decode('utf-8'))
        if os.name == 'nt':
            self.assertTrue(captured['flags'])
        else:
            self.assertTrue(captured['session'])

    def test_plain_executable_no_bridge(self):
        captured = {}

        def popen(argv, **kwargs):
            captured['argv'] = list(argv)
            return type('P', (), {'pid': 1})

        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.fire(dry_run=False, popen_fn=popen, which_fn=lambda x: '/usr/bin/claude')
        self.assertEqual(['/usr/bin/claude', '-p', '--dangerously-skip-permissions'],
                         captured['argv'])

    def test_spawn_failure_is_error_and_advances_cooldown(self):
        def popen(*a, **k):
            raise OSError('no such executable')

        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        decision = self.fire(dry_run=False, popen_fn=popen)
        self.assertEqual('ERROR', decision['event'])
        state = self.read_state()
        self.assertGreaterEqual(state['last_fired'], NOW)
        self.assertIn('1007', state['seen'])


class SpawnLogTests(unittest.TestCase):
    """One round, one log: the CLI output of a manual round lands in a file
    keyed by its execution id, so the run-detail view can stream it and a
    dead-on-arrival process still leaves its own evidence. The cron path
    (no execution id) keeps the shared trigger-spawn.log."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {'spawn': {'command': ['claude', '-p']},
                    'repo_cwd': str(self.tmp)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def popen(self, argv, **kwargs):
        kwargs['stdout'].write(b'round output\n')
        return type('P', (), {'pid': 4321})

    def spawn(self, log_path=None):
        return trigger.spawn_worker(self.cfg, str(self.tmp), 'prompt', False,
                                    popen_fn=self.popen,
                                    which_fn=lambda x: '/usr/bin/claude',
                                    log_path=log_path)

    def test_execution_log_path_is_per_execution_and_validated(self):
        exec_id = 'a' * 32
        self.assertEqual(Path(self.tmp) / 'executions' / ('a' * 32 + '.log'),
                         trigger.spawn_log_path(str(self.tmp), exec_id))
        for bad in (None, '', 'x', 'A' * 31, '../escape', 123, True):
            self.assertIsNone(trigger.spawn_log_path(str(self.tmp), bad))

    def test_round_log_receives_the_process_output(self):
        path = trigger.spawn_log_path(str(self.tmp), 'b' * 32)
        info = self.spawn(log_path=path)
        self.assertEqual('round output\n', path.read_text(encoding='utf-8'))
        self.assertEqual(str(path), info['log'])

    def test_shared_trigger_log_stays_the_default(self):
        info = self.spawn()
        shared = self.tmp / trigger.SPAWN_LOG
        self.assertEqual(str(shared), info['log'])
        self.assertEqual('round output\n', shared.read_text(encoding='utf-8'))
        self.assertFalse((self.tmp / 'executions').exists())

    def test_result_carries_the_live_process_for_liveness_checks(self):
        info = self.spawn()
        self.assertEqual(4321, info['pid'])
        self.assertEqual(4321, info['proc'].pid)

    def test_dry_run_writes_nothing(self):
        info = trigger.spawn_worker(
            self.cfg, str(self.tmp), 'prompt', True,
            which_fn=lambda x: '/usr/bin/claude',
            log_path=trigger.spawn_log_path(str(self.tmp), 'c' * 32))
        self.assertIsNone(info['pid'])
        self.assertIsNone(info['log'])
        self.assertFalse((self.tmp / 'executions').exists())


class LogTests(Harness):
    def test_decisions_appended_as_jsonl(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        self.fire()
        self.rewind_cooldown()
        self.fire()
        lines = (self.state_dir / 'trigger-log.jsonl').read_text(
            encoding='utf-8').strip().splitlines()
        events = [json.loads(x)['event'] for x in lines]
        self.assertEqual(['SPAWNED_DRYRUN', 'SKIPPED_NO_MATCH'], events)
        self.assertNotIn('prompt', json.loads(lines[0]))


class CliTests(Harness):
    def cli(self, args, **extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = trigger.main(args,
                                gitea_factory=lambda p: self.gitea,
                                service_factory=lambda c: self.service,
                                config_provider=self.provider, now=NOW, **extra)
        return code, out.getvalue()

    def test_check_prints_single_line_json_exit_zero(self):
        self.gitea = FakeGitea([plain_issue(7, '[BUG] x')])
        code, out = self.cli(['--config', str(self.cfg()), 'check'])
        self.assertEqual(0, code)
        payload = json.loads(out.strip())
        self.assertEqual('MATCHED', payload['event'])
        self.assertEqual(1, len(out.strip().splitlines()))

    def test_missing_config_file_exits_nonzero(self):
        code, out = self.cli(['--config', str(self.tmp / 'nope.json'), 'check'])
        self.assertEqual(1, code)
        self.assertFalse(json.loads(out.strip())['ok'])

    def test_bad_arguments_rejected(self):
        with self.assertRaises(SystemExit):
            self.cli(['--config', str(self.cfg()), 'explode'])



class PromptFileCollisionTests(unittest.TestCase):
    """One round per prompt file. Observed 2026-09-18 on the GPU machine:
    two review rounds spawned seconds apart shared one fixed-name stdin
    file, the later spawn truncated it under the earlier session, and the
    two executions' reported streams both carried the SAME session —
    i.e. a round reviewed the wrong PR and its verdict would land on the
    wrong pull request. The file is per-round state, never a shared slot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='spawn-collision-'))
        (self.tmp / 'repo').mkdir()
        self.cfg = {"spawn": {"command": ["claude", "-p"]},
                    'repo_cwd': str(self.tmp / 'repo')}
        self.state = str(self.tmp / 'state')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def spawn(self, prompt, seen, key=None):
        """Spawn with a fake child that records the stdin FILE (not its
        bytes): a real child reads it after the parent returned, which is
        exactly the window the collision lives in."""
        def popen(argv, cwd=None, stdin=None, stdout=None, stderr=None, **kw):
            seen.append(stdin.name)
            return type('P', (), {'pid': 4242})()
        return trigger.spawn_worker(self.cfg, self.state, prompt, False,
                                    popen_fn=popen, prompt_key=key)

    def test_each_round_gets_its_own_prompt_file(self):
        seen = []
        self.spawn('PROMPT-ONE execution=%s' % ('a' * 32), seen, key='a' * 32)
        self.spawn('PROMPT-TWO execution=%s' % ('b' * 32), seen, key='b' * 32)
        self.assertNotEqual(seen[0], seen[1],
                            'rounds must never share one prompt file')
        self.assertEqual('PROMPT-ONE execution=%s' % ('a' * 32),
                         Path(seen[0]).read_text(encoding='utf-8'))
        self.assertEqual('PROMPT-TWO execution=%s' % ('b' * 32),
                         Path(seen[1]).read_text(encoding='utf-8'))

    def test_prompt_file_name_carries_the_round_key(self):
        seen = []
        self.spawn('X', seen, key='deadbeefdeadbeef')
        self.assertIn('deadbeefdeadbeef', Path(seen[0]).name)

    def test_prompt_file_name_survives_a_hostile_key(self):
        seen = []
        self.spawn('X', seen, key='../../evil:key?')
        self.assertEqual(str(Path(self.state)), str(Path(seen[0]).parent))

    def test_stale_prompt_files_are_pruned_fresh_ones_kept(self):
        stale = Path(self.state) / 'trigger-prompt-oldround.txt'
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('old', encoding='utf-8')
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))
        seen = []
        self.spawn('FRESH', seen, key='freshrnd')
        self.assertFalse(stale.exists(), 'a month-old prompt copy must not pile up')
        self.assertTrue(Path(seen[0]).exists(), 'this round\'s own file must survive')

if __name__ == '__main__':
    unittest.main()
