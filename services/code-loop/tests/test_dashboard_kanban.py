#!/usr/bin/env python3
"""Kanban board tests: the seven-column board derived from collected facts
(split from test_dashboard_store.py to stay under its line-count ratchet
baseline — issue #185 convention: split, don't raise).

GET /api/kanban serves this board; the review column's card carries the manual
trigger's outcome (``trigger``) and whether the control plane can derive a
review item for the PR at all (``review_scope``)."""
import json
import sqlite3
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import store
from _dashboard_store_base import (StoreTestCase, comment, epoch, gitea_issue,
                                   gitea_pr, ts)


class KanbanTests(StoreTestCase):
    """store.kanban: the seven-column board derived from collected facts.

    Column keys: todo / running / review / reviewing / ready / merged /
    blocked. The dedup invariant: an issue number appears in at most one of
    todo/running/blocked (RUNNING reference > needs-human > todo); a PR
    appears in at most one of review/reviewing/ready/merged/blocked (a live
    reviewer execution outranks the verdict, mutually exclusive)."""

    def load(self, issues=(), prs=(), runtime=None):
        pid = self.pid()
        store.apply_snapshot(self.db, pid,
                             {'issues': list(issues), 'prs': list(prs), 'base_sha': 'b'})
        if runtime:
            store.replace_runtime(self.db, pid, runtime)
        return pid

    def item(self, cp_id, kind, key, payload):
        return {'id': cp_id, 'key': key, 'kind': kind, 'state': None,
                'priority': None, 'version': None, 'payload': json.dumps(payload),
                'resources': None, 'created_at': ts(5)}

    def execution(self, cp_id, item_cp_id, state='RUNNING', created=ts(5), **extra):
        row = {'id': cp_id, 'item_id': item_cp_id, 'worker': 'worker-x',
               'state': state, 'result': None, 'worktree': 'D:/wt', 'branch': 'fix/mocx/x',
               'head': 'abc', 'expires_at': None, 'created_at': created}
        row.update(extra)
        return row

    def columns(self, **kw):
        board = store.kanban(self.db, **kw)
        self.assertEqual({'todo', 'running', 'review', 'reviewing', 'ready',
                          'merged', 'blocked'}, set(board))
        return board

    def reviewer_runtime(self, number, state='RUNNING', created=ts(5),
                         payload=None, **extra):
        """A reviewer work item (review:<n>) with an execution, the CP shape
        that sync derives for every open PR."""
        item = self.item(900 + number % 50, 'reviewer', 'review:%d' % number,
                         payload if payload is not None
                         else {'pr': {'number': number, 'title': 'PR %d' % number,
                                      'html_url': 'http://g/pr/%d' % number}})
        return {'work_items': [item],
                'executions': [self.execution(950 + number % 40,
                                              item['id'], state=state, created=created,
                                              **extra)],
                'leases': [], 'outbox': [], 'events': []}

    def live_cp(self, executions, commands=None, base=None):
        """Write the project's machine-local control-plane store at the path
        views derives from config_path (minimal CP shape: work_items +
        executions) and point the mirror row at it. Executions are reviewer
        runs; each entry is {'number', 'expires_at', ...}.

        ``commands`` (each {'number', 'state', ...}) writes the ledger table the
        trigger outcome is read from — omitted it leaves the table out entirely,
        which is the pre-ledger store shape. ``base`` writes the project's
        control-plane config (a dict is written verbatim, e.g. ``{}`` for a
        config that carries no base key); omitted the config stays unreadable."""
        cfg = Path(self.tmp.name) / 'cp.json'
        cp_db = cfg.parent / 'state' / 'control-plane.sqlite'
        cp_db.parent.mkdir(parents=True, exist_ok=True)
        if base is not None:
            cfg.write_text(json.dumps(base if isinstance(base, dict) else {'base': base}),
                           encoding='utf-8')
        con = sqlite3.connect(str(cp_db))
        con.executescript(
            'CREATE TABLE work_items (id TEXT PRIMARY KEY, key TEXT, kind TEXT, payload TEXT);'
            'CREATE TABLE executions (id TEXT PRIMARY KEY, item_id TEXT, worker TEXT,'
            ' branch TEXT, created_at REAL, expires_at REAL, state TEXT, worktree TEXT);'
            + ('' if commands is None else
               'CREATE TABLE commands (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT,'
               ' number INTEGER, state TEXT, claimed_by TEXT, fail_reason TEXT,'
               ' created_at REAL, execution TEXT, agent TEXT, model TEXT);'))
        for row in executions:
            item_id = 'item-%d' % row['number']
            con.execute('INSERT INTO work_items VALUES (?,?,?,?)',
                        (item_id, 'review:%d' % row['number'], 'reviewer',
                         json.dumps({'pr': {'number': row['number'],
                                            'title': 'PR %d' % row['number'],
                                            'html_url': 'http://g/pr/%d' % row['number']}})))
            con.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)',
                        ('ex-%d' % row['number'], item_id, row.get('worker', 'h/reviewer'),
                         row.get('branch', ''), epoch(20, 11), row['expires_at'], 'RUNNING',
                         row.get('worktree', '')))
        for i, row in enumerate(commands or [], 1):
            con.execute('INSERT INTO commands (id, action, number, state, claimed_by,'
                        ' fail_reason, created_at, execution, agent, model)'
                        ' VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (row.get('id', i), row.get('action', 'review_round'), row['number'],
                         row['state'], row.get('claimed_by'), row.get('fail_reason'),
                         row.get('created_at', epoch(20, 10)), row.get('execution'),
                         row.get('agent'), row.get('model')))
        con.commit()
        con.close()
        store.upsert_project(self.db, 'a__b', str(cfg))

    def test_reviewing_reads_the_live_control_plane_store(self):
        """The board renders the reviewing column from the machine-local CP
        store: a claim shows up without any collect cycle, the card carries the
        prepared worktree, and the PR slot is owned by the live execution."""
        self.load(prs=[gitea_pr(88)])
        self.live_cp([{'number': 88, 'expires_at': epoch(20, 13),
                       'worktree': 'D:/wt/review-pr-88'}])
        board = self.columns(now=self.now)
        self.assertEqual([88], [c['number'] for c in board['reviewing']])
        self.assertEqual('h/reviewer', board['reviewing'][0]['worker'])
        self.assertEqual('D:/wt/review-pr-88', board['reviewing'][0]['worktree'])
        self.assertFalse(board['reviewing'][0]['lease_expired'])
        self.assertEqual([], board['review'])

    def test_live_store_replaces_a_stale_mirror_row(self):
        self.load(prs=[gitea_pr(88)], runtime=self.reviewer_runtime(88))
        self.live_cp([])   # live truth: nothing is running for this project
        board = self.columns(now=self.now)
        self.assertEqual([], board['reviewing'])
        self.assertEqual([88], [c['number'] for c in board['review']])

    def test_expired_lease_is_not_reviewing(self):
        """Same liveness rule the CP reclaims by: an expired lease is a reclaim
        target, not running work."""
        self.load(prs=[gitea_pr(88)])
        self.live_cp([{'number': 88, 'expires_at': epoch(20, 11)}])
        board = self.columns(now=self.now)
        self.assertEqual([], board['reviewing'])
        self.assertEqual([88], [c['number'] for c in board['review']])

    def test_unreadable_live_store_falls_back_to_the_mirror(self):
        """The fallback keeps the collected-mirror semantics unchanged — an
        expired lease row still renders with its chip."""
        self.load(prs=[gitea_pr(88)], runtime=self.reviewer_runtime(88, expires_at=ts(5)))
        self.live_cp([{'number': 88, 'expires_at': epoch(20, 13)}])
        (Path(self.tmp.name) / 'state' / 'control-plane.sqlite').write_bytes(b'not a sqlite file')
        board = self.columns(now=self.now)
        self.assertEqual([88], [c['number'] for c in board['reviewing']])
        self.assertTrue(board['reviewing'][0]['lease_expired'])
        self.assertEqual([], board['review'])

    def test_missing_live_store_falls_back_to_the_mirror(self):
        self.load(prs=[gitea_pr(88)], runtime=self.reviewer_runtime(88))
        store.upsert_project(self.db, 'a__b', str(Path(self.tmp.name) / 'cp.json'))
        board = self.columns(now=self.now)
        self.assertEqual([88], [c['number'] for c in board['reviewing']])

    def test_review_card_carries_the_latest_trigger_command(self):
        """The click's outcome is read back from the command ledger (the shape
        the card renders: state / reason / when) — the board shows where the
        round went without the page having to remember it."""
        self.load(prs=[gitea_pr(88)])
        self.live_cp([], commands=[{'number': 88, 'state': 'FAILED',
                                    'fail_reason': 'CLAIM_UNAVAILABLE: review:88 不存在',
                                    'created_at': epoch(20, 10)}])
        card = self.columns(now=self.now)['review'][0]
        self.assertEqual({'state': 'FAILED',
                          'fail_reason': 'CLAIM_UNAVAILABLE: review:88 不存在',
                          'at': epoch(20, 10)}, card['trigger'])

    def test_trigger_is_the_newest_command_for_that_pr_only(self):
        self.load(prs=[gitea_pr(88), gitea_pr(99)])
        self.live_cp([], commands=[
            {'id': 1, 'number': 88, 'state': 'FAILED', 'fail_reason': 'COMMAND_TTL'},
            {'id': 2, 'number': 99, 'state': 'FAILED', 'fail_reason': 'COMMAND_TTL'},
            {'id': 3, 'number': 88, 'state': 'PENDING', 'created_at': epoch(20, 11)}])
        cards = {c['number']: c for c in self.columns(now=self.now)['review']}
        self.assertEqual('PENDING', cards[88]['trigger']['state'])
        self.assertIsNone(cards[88]['trigger']['fail_reason'])
        self.assertEqual(epoch(20, 11), cards[88]['trigger']['at'])
        self.assertEqual('COMMAND_TTL', cards[99]['trigger']['fail_reason'])

    def test_trigger_is_absent_when_the_ledger_cannot_be_read(self):
        """Fail-open in every unreadable shape: an unregistered config path, a
        missing store, a pre-ledger store without the commands table, and a
        corrupt store. None of them may invent an outcome — and a missing
        ledger must not take the scope verdict down with it."""
        self.load(prs=[gitea_pr(88, base={'sha': 'b', 'ref': 'dev'})])
        self.db.execute("UPDATE projects SET config_path=''")
        card = self.columns(now=self.now)['review'][0]
        self.assertIsNone(card['trigger'])
        self.assertIsNone(card['review_scope'])
        store.upsert_project(self.db, 'a__b', str(Path(self.tmp.name) / 'gone.json'))
        self.assertIsNone(self.columns(now=self.now)['review'][0]['trigger'])
        self.live_cp([], base='master')   # 库与配置都在，但没有 commands 表
        card = self.columns(now=self.now)['review'][0]
        self.assertIsNone(card['trigger'])
        self.assertFalse(card['review_scope'])   # 台账缺失不影响范围判定
        (Path(self.tmp.name) / 'state' / 'control-plane.sqlite').write_bytes(b'not sqlite')
        card = self.columns(now=self.now)['review'][0]
        self.assertIsNone(card['trigger'])
        self.assertFalse(card['review_scope'])   # 库坏同样不影响范围判定（配置是另一个文件）

    def test_review_scope_flags_a_pr_outside_the_project_base(self):
        """A PR whose base is not the project's control-plane base can never
        get a review item derived (cp/service.py) — the board says so instead
        of offering a button that must fail."""
        self.load(prs=[gitea_pr(88, base={'sha': 'b', 'ref': 'dev'})])
        self.live_cp([], base='master')
        card = self.columns(now=self.now)['review'][0]
        self.assertFalse(card['review_scope'])
        self.assertIn('base=dev', card['review_scope_note'])
        self.assertIn('master', card['review_scope_note'])

    def test_review_scope_is_true_inside_the_project_base(self):
        self.load(prs=[gitea_pr(88, base={'sha': 'b', 'ref': 'master'})])
        self.live_cp([], base='master')
        card = self.columns(now=self.now)['review'][0]
        self.assertTrue(card['review_scope'])
        self.assertEqual('', card['review_scope_note'])

    def test_review_scope_defaults_a_missing_base_like_the_control_plane(self):
        """cp/runtime.py setdefaults an absent base key to 'main', so a project
        configured without one derives review items for main-based PRs — the
        board must not call those out of scope."""
        self.load(prs=[gitea_pr(88, base={'sha': 'b', 'ref': 'main'}),
                       gitea_pr(89, base={'sha': 'b', 'ref': 'dev'})])
        self.live_cp([], base={})      # 配置文件在，但没有 base 键
        cards = {c['number']: c for c in self.columns(now=self.now)['review']}
        self.assertTrue(cards[88]['review_scope'])
        self.assertFalse(cards[89]['review_scope'])

    def test_trigger_keys_by_project_and_number(self):
        """One read spans every project, so the ledger state must be keyed by
        (project, number): two projects holding PR 88 must not show each
        other's click outcome."""
        self.load(prs=[gitea_pr(88)])
        self.live_cp([], commands=[{'number': 88, 'state': 'FAILED',
                                    'fail_reason': 'COMMAND_TTL'}])
        pid2 = store.upsert_project(self.db, 'c__d', 'cp2.json')
        store.apply_snapshot(self.db, pid2, {'issues': [], 'prs': [gitea_pr(88)],
                                             'base_sha': 'b'})
        board = self.columns(now=self.now)
        by_project = {(c['org_repo'], c['number']): c['trigger'] for c in board['review']}
        self.assertEqual('COMMAND_TTL', by_project[('a__b', 88)]['fail_reason'])
        self.assertIsNone(by_project[('c__d', 88)])

    def test_review_scope_is_unknown_without_a_readable_config(self):
        """Unknown is not False: an unreadable project config must keep the
        button live (fail-open), never disable it on a guess."""
        self.load(prs=[gitea_pr(88, base={'sha': 'b', 'ref': 'dev'})])
        self.live_cp([], base=None)      # 配置文件不存在
        card = self.columns(now=self.now)['review'][0]
        self.assertIsNone(card['review_scope'])
        self.assertEqual('', card['review_scope_note'])

    def test_review_scope_is_unknown_without_a_collected_base_ref(self):
        self.load(prs=[gitea_pr(88, base={'sha': 'b', 'ref': ''})])
        self.live_cp([], base='master')
        self.assertIsNone(self.columns(now=self.now)['review'][0]['review_scope'])

    def test_empty_db_yields_seven_empty_columns(self):
        self.pid()
        board = self.columns()
        self.assertTrue(all(v == [] for v in board.values()))

    def test_todo_open_issues_only(self):
        self.load(issues=[gitea_issue(1), gitea_issue(2, state='closed'),
                          gitea_issue(3, title='[❓needs-human] pick')])
        board = self.columns(now=self.now)
        self.assertEqual([1], [c['number'] for c in board['todo']])
        self.assertEqual('issue', board['todo'][0]['card'])
        self.assertEqual('a__b', board['todo'][0]['org_repo'])

    def test_running_execution_card_and_issue_dedup(self):
        runtime = {'work_items': [self.item(7, 'handler', 'issue:16', {'issue': {'number': 16}})],
                   'executions': [self.execution(8, 7)],
                   'leases': [], 'outbox': [], 'events': []}
        self.load(issues=[gitea_issue(16)], runtime=runtime)
        board = self.columns(now=self.now)
        self.assertEqual([16], [c['issue_number'] for c in board['running']])
        card = board['running'][0]
        self.assertEqual('exec', card['card'])
        self.assertEqual('worker-x', card['worker'])
        self.assertEqual('fix/mocx/x', card['branch'])
        self.assertFalse(card['lease_expired'])
        self.assertEqual([], board['todo'])  # the running issue never re-appears as todo

    def test_running_execution_without_issue_payload(self):
        """A handler execution whose payload lacks an issue reference still
        shows as a running card (key fallback), without breaking the board."""
        runtime = {'work_items': [self.item(7, 'handler', 'issue:?', {})],
                   'executions': [self.execution(8, 7)],
                   'leases': [], 'outbox': [], 'events': []}
        self.load(runtime=runtime)
        board = self.columns(now=self.now)
        self.assertEqual(1, len(board['running']))
        self.assertIsNone(board['running'][0]['issue_number'])

    def test_running_beats_pr_link_and_needs_human(self):
        """One issue referenced by a RUNNING execution, linked by an open PR
        and tagged needs-human: exactly one issue-level card, in running."""
        runtime = {'work_items': [self.item(7, 'handler', 'issue:16', {'issue': {'number': 16}})],
                   'executions': [self.execution(8, 7)],
                   'leases': [], 'outbox': [], 'events': []}
        self.load(issues=[gitea_issue(16, title='[❓needs-human] fix 16')],
                  prs=[gitea_pr(101, body='Closes #16')], runtime=runtime)
        board = self.columns(now=self.now)
        self.assertEqual([16], [c['issue_number'] for c in board['running']])
        self.assertEqual([], board['todo'])
        self.assertNotIn('decision', [c['card'] for c in board['blocked']])
        # the linked PR itself is still a PR-level card awaiting review
        self.assertEqual([101], [c['number'] for c in board['review']])

    def test_only_handler_executions_in_running(self):
        """处理中 is the development column: RUNNING reviewer/discover
        executions do not create cards here (reviewer activity owns the
        reviewing column; discover activity lives on the loops view)."""
        runtime = {'work_items': [
            self.item(7, 'handler', 'issue:16', {'issue': {'number': 16}}),
            self.item(9, 'reviewer', 'review:88', {'pr': {'number': 88}}),
            self.item(11, 'discover', 'discover', {'task': 'scan'})],
            'executions': [self.execution(8, 7), self.execution(10, 9),
                           self.execution(12, 11)],
            'leases': [], 'outbox': [], 'events': []}
        self.load(issues=[gitea_issue(16)],
                  prs=[gitea_pr(88)], runtime=runtime)
        board = self.columns(now=self.now)
        self.assertEqual([16], [c['issue_number'] for c in board['running']])
        self.assertEqual([88], [c['number'] for c in board['reviewing']])
        self.assertEqual([], board['review'])

    def test_reviewer_running_card_carries_pr_identity(self):
        """审核中: a RUNNING reviewer execution projects as an exec card with
        the PR identity (number/title/html_url), worker, branch and the lease
        flag — everything the operator needs to see who is reviewing what."""
        self.load(prs=[gitea_pr(88)],
                  runtime=self.reviewer_runtime(88, branch='feature/pr-88'))
        board = self.columns(now=self.now)
        card = board['reviewing'][0]
        self.assertEqual('exec', card['card'])
        self.assertEqual('reviewer', card['kind'])
        self.assertEqual(88, card['number'])
        self.assertEqual('PR 88', card['title'])
        self.assertEqual('http://g/pr/88', card['html_url'])
        self.assertEqual('review:88', card['key'])
        self.assertEqual('worker-x', card['worker'])
        self.assertEqual('feature/pr-88', card['branch'])
        self.assertEqual('a__b', card['org_repo'])
        self.assertFalse(card['lease_expired'])

    def test_review_finished_pr_returns_to_review(self):
        """When the reviewer execution is no longer RUNNING the PR is back in
        review (verdict decides again) and reviewing is empty."""
        self.load(prs=[gitea_pr(88)],
                  runtime=self.reviewer_runtime(88, state='SUCCEEDED'))
        board = self.columns(now=self.now)
        self.assertEqual([88], [c['number'] for c in board['review']])
        self.assertEqual([], board['reviewing'])

    def test_reviewing_beats_stale_verdict(self):
        """A PR being re-reviewed (earlier Changes requested) belongs to
        审核中 while the reviewer runs — column exclusivity extends past the
        verdict split."""
        self.load(prs=[gitea_pr(102, comments=[comment('Changes requested', 3)])],
                  runtime=self.reviewer_runtime(102))
        board = self.columns(now=self.now)
        self.assertEqual([102], [c['number'] for c in board['reviewing']])
        self.assertEqual([], board['review'])
        self.assertEqual([], board['blocked'])

    def test_review_exec_without_pr_payload(self):
        """A reviewer execution whose payload lacks a PR still renders (key
        fallback, number None) and never pulls PR cards out of review."""
        runtime = self.reviewer_runtime(0, payload={})
        runtime['work_items'][0]['key'] = 'review:?'
        self.load(prs=[gitea_pr(88)], runtime=runtime)
        board = self.columns(now=self.now)
        self.assertEqual(1, len(board['reviewing']))
        self.assertIsNone(board['reviewing'][0]['number'])
        self.assertEqual('review:?', board['reviewing'][0]['key'])
        self.assertEqual([88], [c['number'] for c in board['review']])

    def test_reviewing_sorted_by_started(self):
        """Oldest reviewer execution first, matching the running column."""
        first, second = self.reviewer_runtime(89, created=ts(3)), self.reviewer_runtime(88)
        self.load(prs=[gitea_pr(88), gitea_pr(89)], runtime={
            'work_items': first['work_items'] + second['work_items'],
            'executions': first['executions'] + second['executions'],
            'leases': [], 'outbox': [], 'events': []})
        board = self.columns(now=self.now)
        self.assertEqual([89, 88], [c['number'] for c in board['reviewing']])

    def test_reviewing_lease_expired_flag(self):
        self.load(prs=[gitea_pr(88)],
                  runtime=self.reviewer_runtime(88, expires_at=str(epoch(5) + 3600)))
        board = self.columns(now=epoch(5) + 7200)
        self.assertTrue(board['reviewing'][0]['lease_expired'])
        board = self.columns(now=epoch(5) + 1800)
        self.assertFalse(board['reviewing'][0]['lease_expired'])

    def test_lease_expired_flag(self):
        runtime = {'work_items': [self.item(7, 'handler', 'issue:16', {'issue': {'number': 16}})],
                   'executions': [self.execution(8, 7, expires_at=str(epoch(5) + 3600))],
                   'leases': [], 'outbox': [], 'events': []}
        self.load(issues=[gitea_issue(16)], runtime=runtime)
        board = self.columns(now=epoch(5) + 7200)
        self.assertTrue(board['running'][0]['lease_expired'])
        board = self.columns(now=epoch(5) + 1800)
        self.assertFalse(board['running'][0]['lease_expired'])

    def test_review_ready_blocked_by_verdict(self):
        self.load(prs=[gitea_pr(101),
                       gitea_pr(102, comments=[comment('Changes requested', 3)]),
                       gitea_pr(103, comments=[comment('Approved for manual merge', 3)]),
                       gitea_pr(104, comments=[comment('Blocked', 3)]),
                       gitea_pr(105, comments=[comment('Validation failed', 3)])])
        board = self.columns(now=self.now)
        self.assertEqual([101, 102], sorted(c['number'] for c in board['review']))
        requested = next(c for c in board['review'] if c['number'] == 102)
        self.assertEqual('Changes requested', requested['verdict'])
        self.assertIsNone(next(c for c in board['review'] if c['number'] == 101)['verdict'])
        self.assertEqual([103], [c['number'] for c in board['ready']])
        self.assertAlmostEqual(self.now - epoch(3), board['ready'][0]['waiting_seconds'], delta=1)
        self.assertEqual([104, 105], sorted(c['number'] for c in board['blocked']))
        self.assertEqual({'pr'}, {c['card'] for c in board['blocked']})

    def test_ready_carries_verdict_body_for_actions(self):
        """The ready column doubles as the action queue since the actions
        view merged into the board: cards carry the verdict body so the UI
        can offer the inline review-verdict fold without a second fetch."""
        self.load(prs=[gitea_pr(103, comments=[
            comment('Approved for manual merge\n\n细节正文', 3)])])
        board = self.columns(now=self.now)
        self.assertIn('细节正文', board['ready'][0]['verdict_body'])

    def test_ready_sorted_by_waiting_desc(self):
        """Longest-waiting approved PR first: merging is the human bottleneck,
        so the board surfaces the oldest verdict at the top."""
        self.load(prs=[gitea_pr(101, comments=[comment('Approved for manual merge', 5)]),
                       gitea_pr(102, comments=[comment('Approved for manual merge', 3)]),
                       gitea_pr(103, comments=[comment('Approved for manual merge', 8)])])
        board = self.columns(now=self.now)
        self.assertEqual([102, 101, 103], [c['number'] for c in board['ready']])

    def test_merged_recent_first_capped(self):
        merged = [gitea_pr(n, state='closed', merged=True, merged_at=ts(3 + n - 100))
                  for n in range(100, 118)]  # 18 merged PRs, merged_at ascending
        self.load(prs=merged)
        board = self.columns(now=self.now)
        self.assertEqual(15, len(board['merged']))
        self.assertEqual(117, board['merged'][0]['number'])  # newest first
        self.assertEqual(103, board['merged'][-1]['number'])  # cap drops the oldest

    def test_merged_cap_is_global_across_projects(self):
        """The MERGED_CAP applies to the whole scope: two projects' merges are
        ranked together, not 15 per project."""
        self.load(prs=[gitea_pr(100 + n, state='closed', merged=True,
                                merged_at=ts(1 + n)) for n in range(10)])  # a__b: 100..109
        pid2 = store.upsert_project(self.db, 'c__d', 'cp2.json')
        store.apply_snapshot(self.db, pid2, {
            'issues': [], 'prs': [gitea_pr(200 + n, state='closed', merged=True,
                                           merged_at=ts(11 + n)) for n in range(10)],
            'base_sha': 'b'})  # c__d: 200..209, all newer than a__b's
        board = self.columns(now=self.now)
        self.assertEqual(15, len(board['merged']))
        self.assertEqual({209, 208, 207, 206, 205},
                         {c['number'] for c in board['merged'][:5]})  # newest first
        self.assertEqual(5, len([c for c in board['merged'] if c['org_repo'] == 'a__b']))
        self.assertEqual(105, board['merged'][-1]['number'])  # cap cut a__b's oldest five

    def test_needs_human_replied_returns_to_todo(self):
        """A needs-human issue the user already answered left the decision
        queue (ball with the handler): it is pending handler intake again,
        so it shows as todo, not blocked."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one')])
        store.record_user_reply(self.db, pid, 1, '1A 本期做', ts(6))
        board = self.columns(now=self.now)
        self.assertEqual([1], [c['number'] for c in board['todo']])
        self.assertEqual([], board['blocked'])

    def test_blocked_decisions_and_outbox(self):
        runtime = {'work_items': [], 'executions': [], 'leases': [],
                   'outbox': [{'id': 1, 'execution_id': 9, 'action': 'comment_issue',
                               'state': 'SENDING', 'receipt': None, 'created_at': ts(5)},
                              {'id': 2, 'execution_id': 9, 'action': 'create_pr',
                               'state': 'UNKNOWN', 'receipt': None, 'created_at': ts(5)},
                              {'id': 3, 'execution_id': 9, 'action': 'comment_issue',
                               'state': 'SENT', 'receipt': '{}', 'created_at': ts(5)}],
                   'events': []}
        self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one')], runtime=runtime)
        board = self.columns(now=self.now)
        cards = board['blocked']
        self.assertEqual({'decision', 'outbox'}, {c['card'] for c in cards})
        self.assertEqual([1], [c['number'] for c in cards if c['card'] == 'decision'])
        self.assertEqual({'SENDING', 'UNKNOWN'},
                         {c['state'] for c in cards if c['card'] == 'outbox'})

    def test_org_filter_and_unknown_raises(self):
        """Scoping is per (project, number): two projects with the same issue
        number each see exactly their own card; a project without cards gets
        seven empty columns."""
        self.load(issues=[gitea_issue(1)])
        pid2 = store.upsert_project(self.db, 'c__d', 'cp2.json')
        store.apply_snapshot(self.db, pid2,
                             {'issues': [gitea_issue(1, title='other')], 'prs': [],
                              'base_sha': 'b'})
        board = self.columns(org_repo='a__b', now=self.now)
        self.assertEqual([('a__b', 1)], [(c['org_repo'], c['number']) for c in board['todo']])
        board2 = self.columns(org_repo='c__d', now=self.now)
        self.assertEqual([('c__d', 1)], [(c['org_repo'], c['number']) for c in board2['todo']])
        store.upsert_project(self.db, 'e__f', 'cp3.json')
        empty = self.columns(org_repo='e__f', now=self.now)
        self.assertTrue(all(v == [] for v in empty.values()))
        with self.assertRaises(KeyError):
            store.kanban(self.db, org_repo='zz__zz')




class RepairBoardTests(KanbanTests):
    """The repair round's board facts (spec 2026-09-20-dash-conflict-repair-
    trigger): RUNNING repair:<n> runs project live into 处理中, the newest
    repair_round click rides the conflicted PR card (repair_trigger), and the
    live run occupies the PR (repair_busy — the frontend suppresses the
    button)."""

    def repair_cp(self, numbers=(), commands=None):
        """Write the machine-local CP store with handler repair:<n> items and
        RUNNING executions (the same minimal shape the reviewer live_cp
        writes), plus optional repair_round ledger rows."""
        cfg = Path(self.tmp.name) / 'cp.json'
        cp_db = cfg.parent / 'state' / 'control-plane.sqlite'
        cp_db.parent.mkdir(parents=True, exist_ok=True)
        if cp_db.exists():
            cp_db.unlink()   # same tmp serves subtests: start the ledger fresh
        con = sqlite3.connect(str(cp_db))
        try:
            con.executescript(
                'CREATE TABLE IF NOT EXISTS work_items (id TEXT PRIMARY KEY, key TEXT,'
                ' kind TEXT, payload TEXT);'
                'CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY,'
                ' item_id TEXT, worker TEXT, branch TEXT, created_at REAL,'
                ' expires_at REAL, state TEXT, worktree TEXT);'
                'CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT,'
                ' action TEXT, number INTEGER, state TEXT, claimed_by TEXT,'
                ' fail_reason TEXT, created_at REAL, execution TEXT, agent TEXT,'
                ' model TEXT);')
            for n in numbers:
                item_id = 'item-repair-%d' % n
                con.execute('INSERT INTO work_items VALUES (?,?,?,?)',
                            (item_id, 'repair:%d' % n, 'handler',
                             json.dumps({'task': 'PR_CONFLICT_RESOLUTION',
                                         'pr': {'number': n}})))
                con.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)',
                            ('ex-repair-%d' % n, item_id, 'h/handler',
                             'fix/mocx/repair-pr-%d' % n, epoch(20, 11),
                             epoch(20, 13), 'RUNNING', 'D:/wt/repair-pr-%d' % n))
            for i, row in enumerate(commands or [], 1):
                con.execute('INSERT INTO commands (id, action, number, state,'
                            ' claimed_by, fail_reason, created_at, execution, agent,'
                            ' model) VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (i, 'repair_round', row['number'], row['state'],
                             row.get('claimed_by'), row.get('fail_reason'),
                             epoch(20, 10), None, None, None))
            con.commit()
        finally:
            con.close()
        store.upsert_project(self.db, 'a__b', str(cfg))

    def test_running_repair_projects_live_into_running_column(self):
        self.load(prs=[gitea_pr(88)])
        self.repair_cp(numbers=[88])
        board = self.columns(now=self.now)
        cards = board['running']
        self.assertEqual(1, len(cards))
        card = cards[0]
        self.assertEqual('exec', card['card'])
        self.assertEqual('repair:88', card['key'])
        self.assertEqual('ex-repair-88', card['execution'])
        self.assertEqual('fix/mocx/repair-pr-88', card['branch'])
        self.assertFalse(card['lease_expired'])
        self.assertIsNone(card['issue_number'])

    def test_running_repair_occupies_the_pr_card(self):
        self.load(prs=[gitea_pr(88, mergeable=False)])
        self.repair_cp(numbers=[88])
        board = self.columns(now=self.now)
        ready = [c for c in board['ready']]
        self.assertEqual([], ready)   # no verdict yet: review column
        card = board['review'][0]
        self.assertTrue(card['repair_busy'])
        # the live execution id rides the card: the 修复中… button gains a
        # 查看过程 deep link straight into run detail
        self.assertEqual('ex-repair-88', card['repair_exec'])

    def test_repair_click_failure_rides_the_conflicted_card(self):
        self.load(prs=[gitea_pr(88, mergeable=False)])
        self.repair_cp(numbers=[], commands=[
            {'number': 88, 'state': 'FAILED',
             'fail_reason': 'CLAIM_UNAVAILABLE: repair:88 当前不可领取'}])
        board = self.columns(now=self.now)
        card = board['review'][0]
        self.assertEqual('FAILED', card['repair_trigger']['state'])
        self.assertIn('CLAIM_UNAVAILABLE', card['repair_trigger']['fail_reason'])
        self.assertFalse(card['repair_busy'])
        self.assertIsNone(card['repair_exec'])

    def test_repair_click_state_suppressed_when_mergeable(self):
        for mergeable in (True, None):
            with self.subTest(mergeable=mergeable):
                self.load(prs=[gitea_pr(88, mergeable=mergeable)])
                self.repair_cp(numbers=[], commands=[
                    {'number': 88, 'state': 'FAILED', 'fail_reason': 'x'}])
                board = self.columns(now=self.now)
                card = (board['ready'] or board['review'])[0]
                self.assertIsNone(card['repair_trigger'])

    def test_expired_live_repair_is_not_running(self):
        """Only live leases project: an expired execution is a reclaim target
        (the same liveness rule the reviewer column runs on)."""
        self.load(prs=[gitea_pr(88)])
        cfg = Path(self.tmp.name) / 'cp.json'
        store.upsert_project(self.db, 'a__b', str(cfg))
        cp_db = cfg.parent / 'state' / 'control-plane.sqlite'
        cp_db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(cp_db))
        con.executescript(
            'CREATE TABLE work_items (id TEXT PRIMARY KEY, key TEXT, kind TEXT, payload TEXT);'
            'CREATE TABLE executions (id TEXT PRIMARY KEY, item_id TEXT, worker TEXT,'
            ' branch TEXT, created_at REAL, expires_at REAL, state TEXT, worktree TEXT);')
        con.execute('INSERT INTO work_items VALUES (?,?,?,?)',
                    ('item-1', 'repair:88', 'handler', '{}'))
        con.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?)',
                    ('ex-1', 'item-1', 'h/handler', 'fix/x', epoch(20, 11),
                     epoch(20, 11) - 1, 'RUNNING', 'D:/wt'))
        con.commit()
        con.close()
        board = self.columns(now=self.now)
        self.assertEqual([], board['running'])

    def test_mergeable_reaches_the_frontend_as_a_real_bool(self):
        """The collected column is INTEGER/NULL; the board hands the frontend
        a real bool so JS strict equality (mergeable === false) says what it
        means — a raw 0 made the repair button dead code (review BLOCKER)."""
        self.load(prs=[gitea_pr(88, mergeable=False), gitea_pr(89, mergeable=True),
                       gitea_pr(90)])
        board = self.columns(now=self.now)
        cards = {c['number']: c for col in ('review', 'ready')
                 for c in board[col]}
        self.assertIs(False, cards[88]['mergeable'])
        self.assertIs(True, cards[89]['mergeable'])
        self.assertIsNone(cards[90]['mergeable'])

    def test_mirror_fallback_shows_repair_run_without_live_store(self):
        """A project whose CP store cannot be read falls back to the collected
        mirror — the run still shows, just collect-cycle old."""
        self.load(prs=[gitea_pr(88)], runtime={
            'work_items': [self.item(9, 'handler', 'repair:88',
                                     {'task': 'PR_CONFLICT_RESOLUTION',
                                      'pr': {'number': 88}})],
            'executions': [self.execution(10, 9, created=ts(5))],
            'leases': [], 'outbox': [], 'events': []})
        board = self.columns(now=self.now)
        cards = [c for c in board['running'] if c['key'] == 'repair:88']
        self.assertEqual(1, len(cards))
        self.assertEqual('worker-x', cards[0]['worker'])

    def test_development_runs_keep_their_cards_and_never_double_show(self):
        """issue:<n> development runs flow from the mirror as before; a repair
        run mirrored by the collect cycle shows exactly once — the live store
        owns it, the kanban-side mirror must not add a second copy."""
        self.load(issues=[gitea_issue(7)], runtime={
            'work_items': [self.item(9, 'handler', 'issue:7', {'issue': {'number': 7}}),
                           self.item(10, 'handler', 'repair:88', {'pr': {'number': 88}})],
            'executions': [self.execution(11, 9), self.execution(12, 10)],
            'leases': [], 'outbox': [], 'events': []})
        self.repair_cp(numbers=[88])
        board = self.columns(now=self.now)
        keys = sorted(c['key'] for c in board['running'])
        self.assertEqual(['issue:7', 'repair:88'], keys)
