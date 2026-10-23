"""Dashboard store tests: schema, diff upsert, derived views, alerts.

The seven-column board suite lives in test_dashboard_kanban.py; both files share
the fixtures in _dashboard_store_base.py."""
import json
import sqlite3
import tempfile
import unittest
from decimal import localcontext
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import store, verdicts
from codeloop.dash.ingest import _runtime_identity_key
from _dashboard_store_base import (StoreTestCase, agent_comment, comment, epoch,
                                   gitea_issue, gitea_pr, ts)


class ConnectPragmasTests(unittest.TestCase):
    """2026-09-14 collect starvation: connect must absorb transient writer
    contention (15s busy timeout) and switch the store to WAL so dashboard
    readers never block the collector's commits."""

    def test_busy_timeout_and_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = store.connect(Path(tmp) / 'c.sqlite')
            try:
                self.assertEqual(15000, db.execute('PRAGMA busy_timeout').fetchone()[0])
                self.assertEqual('wal',
                                 db.execute('PRAGMA journal_mode').fetchone()[0].lower())
            finally:
                db.close()


class SchemaTests(StoreTestCase):
    def test_upsert_project_is_idempotent(self):
        first = store.upsert_project(self.db, 'a__b', 'cp.json')
        second = store.upsert_project(self.db, 'a__b', 'cp2.json', base='main')
        self.assertEqual(first, second)
        row = self.db.execute('SELECT * FROM projects').fetchone()
        self.assertEqual('cp2.json', row['config_path'])
        self.assertEqual('main', row['base'])

    def test_sync_log_append(self):
        pid = self.pid()
        store.log_sync(self.db, pid, ok=True, stats={'issues': 1}, started=1.0, finished=2.0)
        row = self.db.execute('SELECT * FROM sync_log').fetchone()
        self.assertEqual(1, row['ok'])
        self.assertEqual({'issues': 1}, json.loads(row['stats']))

    def test_legacy_runtime_row_backfills_hash_without_rewriting_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.sqlite'
            legacy = sqlite3.connect(str(path))
            legacy.execute('''CREATE TABLE work_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
                cp_id INTEGER NOT NULL,key TEXT,kind TEXT,state TEXT,priority INTEGER,
                version INTEGER,payload TEXT,resources TEXT,created_at TEXT,
                UNIQUE(project_id,cp_id))''')
            legacy.execute("INSERT INTO work_items(project_id,cp_id,key,kind,state,priority,"
                           "version,payload,resources,created_at) "
                           "VALUES (1,7,'k','handler','READY',1,2,'{}','[]','t')")
            legacy.commit()
            legacy.close()
            db = store.connect(path)
            try:
                runtime = {
                    'work_items': [{'id': '007', 'key': 'k', 'kind': 'handler',
                                    'state': 'READY', 'priority': 1, 'version': 2,
                                    'payload': '{}', 'resources': [], 'created_at': 't'}],
                    'executions': [], 'leases': [], 'outbox': [],
                    'cp_events': [], 'commands': [],
                }
                dml = []
                db.set_trace_callback(dml.append)
                store.replace_runtime(db, 1, runtime)
                db.set_trace_callback(None)
                self.assertEqual(1, db.execute(
                    'SELECT id FROM work_items WHERE project_id=1').fetchone()[0])
                self.assertIsNotNone(db.execute(
                    'SELECT content_hash FROM work_items WHERE project_id=1').fetchone()[0])
                mirror_writes = [sql.lower() for sql in dml if
                                 sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                                 and 'work_items' in sql.lower()]
                self.assertEqual(1, len(mirror_writes))
                self.assertIn('update work_items set content_hash=', mirror_writes[0])
                dml.clear()
                db.set_trace_callback(dml.append)
                store.replace_runtime(db, 1, runtime)
                db.set_trace_callback(None)
                self.assertEqual([], [sql for sql in dml if
                                     sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                                     and any(table in sql.lower() for table in
                                             ('work_items', 'executions', 'leases',
                                              'outbox', 'cp_events', 'commands'))])
            finally:
                db.close()


class SnapshotTests(StoreTestCase):
    def test_first_apply_inserts_everything(self):
        pid = self.pid()
        issue = gitea_issue(1, comments=[comment('hello', 1, 5)])
        stats = store.apply_snapshot(self.db, pid, self.snap(issues=[issue]))
        self.assertEqual(1, stats['issues_added'])
        rows = self.db.execute('SELECT * FROM issues').fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(['x'], json.loads(rows[0]['labels']))
        comments = self.db.execute("SELECT * FROM comments WHERE kind='issue'").fetchall()
        self.assertEqual(1, len(comments))
        self.assertEqual('hello', comments[0]['body'])

    def test_second_apply_unchanged(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, self.snap(issues=[gitea_issue(1)]))
        stats = store.apply_snapshot(self.db, pid, self.snap(issues=[gitea_issue(1)]))
        self.assertEqual(0, stats['issues_added'] + stats['issues_updated'])
        self.assertEqual(1, stats['issues_unchanged'])

    def test_update_refreshes_row_and_comments(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, self.snap(issues=[gitea_issue(1)]))
        changed = gitea_issue(1, state='closed', updated_at=ts(3), closed_at=ts(3),
                              comments=[comment('done', 3)])
        stats = store.apply_snapshot(self.db, pid, self.snap(issues=[changed]))
        self.assertEqual(1, stats['issues_updated'])
        row = self.db.execute('SELECT * FROM issues').fetchone()
        self.assertEqual('closed', row['state'])
        body = self.db.execute("SELECT body FROM comments WHERE kind='issue'").fetchone()[0]
        self.assertEqual('done', body)

    def test_open_item_comments_refreshed_even_when_row_unchanged(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, self.snap(issues=[gitea_issue(1)]))
        # Same updated_at (row hash unchanged) but a new verdict comment arrived.
        store.apply_snapshot(self.db, pid, self.snap(issues=[
            gitea_issue(1, comments=[comment(' Approved for manual merge ', 2)])]))
        body = self.db.execute("SELECT body FROM comments WHERE kind='issue'").fetchone()[0]
        self.assertIn('Approved', body)

    def test_pr_links_issues_and_reviews_stored(self):
        pid = self.pid()
        pr = gitea_pr(101, body='Fixes #7 , closes  #9', reviews=[
            {'user': {'login': 'rev'}, 'state': 'APPROVED', 'body': 'note', 'submitted_at': ts(3)}])
        store.apply_snapshot(self.db, pid, self.snap(prs=[pr]))
        row = self.db.execute('SELECT * FROM prs').fetchone()
        self.assertEqual([7, 9], json.loads(row['issue_numbers']))
        reviews = self.db.execute('SELECT * FROM pr_reviews').fetchall()
        self.assertEqual(1, len(reviews))
        self.assertEqual('rev', reviews[0]['reviewer'])


VERDICTS = ('Approved for manual merge', 'Changes requested', 'Blocked', 'Validation failed')


class VerdictTests(StoreTestCase):
    def verdict_for(self, comments):
        pid = self.pid()
        pr = gitea_pr(101, comments=comments)
        store.apply_snapshot(self.db, pid, self.snap(prs=[pr]))

    def test_no_verdict(self):
        self.verdict_for([comment('looks fine to me', 3)])
        self.assertIsNone(store.latest_verdict(self.db, self.pid(), 101))

    def test_exact_verdicts_detected(self):
        pid = self.pid()
        prs = [gitea_pr(200 + i, comments=[comment(text, 3)])
               for i, text in enumerate(VERDICTS)]
        store.apply_snapshot(self.db, pid, {'issues': [], 'prs': prs, 'base_sha': 'b'})
        for i, text in enumerate(VERDICTS):
            verdict, _, _ = store.latest_verdict(self.db, pid, 200 + i)
            self.assertEqual(text, verdict)

    def test_whitespace_tolerated_and_latest_wins(self):
        self.verdict_for([comment('Approved for manual merge', 3),
                          comment('  Changes requested  ', 5)])
        verdict, at, poster = store.latest_verdict(self.db, self.pid(), 101)
        self.assertEqual('Changes requested', verdict)
        self.assertEqual(epoch(5), at)
        self.assertEqual('reviewer', poster)

    def test_formatted_markdown_comment_matched_by_phrase(self):
        self.verdict_for([comment('## Reviewer 评审结论：Approved for manual merge to main\n\n**内容', 4)])
        verdict, _, _ = store.latest_verdict(self.db, self.pid(), 101)
        self.assertEqual('Approved for manual merge', verdict)

    def test_verdict_detail_includes_body_and_summary(self):
        pid = self.pid()
        pr = gitea_pr(101, comments=[comment(
            '## Reviewer 评审结论：Approved for manual merge to main\n\n细节……', 3)])
        store.apply_snapshot(self.db, pid, self.snap(prs=[pr]))
        d = store.latest_verdict_detail(self.db, pid, 101)
        self.assertEqual('Approved for manual merge', d['verdict'])
        self.assertIn('细节', d['body'])
        rows = store.pending_merges(self.db)
        self.assertIn('细节', rows[0]['verdict_body'])

    def test_control_plane_decision_preferred(self):
        pid = self.pid()
        pr = gitea_pr(101, comments=[comment(
            '## Reviewer 评审结论：Changes requested', 3)])
        store.apply_snapshot(self.db, pid, self.snap(prs=[pr]))
        store.replace_runtime(self.db, pid, {
            'work_items': [{'id': 9, 'key': 'review:101', 'kind': 'reviewer',
                            'state': 'DONE', 'priority': None, 'version': None,
                            'payload': json.dumps({'pr': {'number': 101}}),
                            'resources': None, 'created_at': ts(4)}],
            'executions': [{'id': 10, 'item_id': 9, 'worker': 'rev-agent',
                            'state': 'SUCCEEDED',
                            'result': json.dumps({'decision': 'Approved for manual merge',
                                                  'reviewer': 'rev-agent', 'summary': 'ok'}),
                            'worktree': None, 'branch': None, 'head': 's101',
                            'expires_at': None, 'created_at': ts(4)}],
            'leases': [], 'outbox': [], 'events': []})
        verdict, at, poster = store.latest_verdict(self.db, pid, 101)
        self.assertEqual('Approved for manual merge', verdict)
        self.assertEqual(epoch(4), at)
        self.assertEqual('rev-agent', poster)
        rows = store.pending_merges(self.db)
        self.assertEqual([101], [r['number'] for r in rows])

    def _handler_runtime(self, pid, decision, day):
        store.replace_runtime(self.db, pid, {
            'work_items': [{'id': 9, 'key': 'repair:101', 'kind': 'handler',
                            'state': 'DONE', 'priority': None, 'version': None,
                            'payload': json.dumps({'pr': {'number': 101}}),
                            'resources': None, 'created_at': ts(day)}],
            'executions': [{'id': 10, 'item_id': 9, 'worker': 'handler-agent',
                            'state': 'SUCCEEDED',
                            'result': json.dumps({'decision': decision,
                                                  'reviewer': 'review-main-pr101',
                                                  'summary': 'blocked by worktree'}),
                            'worktree': None, 'branch': None, 'head': 's101',
                            'expires_at': None, 'created_at': ts(day)}],
            'leases': [], 'outbox': [], 'events': []})

    def test_handler_blocked_execution_maps_to_blocked_verdict(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, self.snap(prs=[gitea_pr(101)]))
        self._handler_runtime(pid, 'BLOCKED', 4)
        d = store.latest_verdict_detail(self.db, pid, 101)
        self.assertEqual('Blocked', d['verdict'])
        self.assertIn('blocked by worktree', d['body'])
        self.assertTrue(any(i['type'] == 'pr' and i['number'] == 101
                            for i in store.exceptions(self.db)))

    def test_handler_approve_execution_is_not_a_verdict(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, self.snap(prs=[gitea_pr(101)]))
        self._handler_runtime(pid, 'APPROVE', 4)
        self.assertIsNone(store.latest_verdict(self.db, pid, 101))

    def test_newer_reviewer_comment_overrides_handler_blocked(self):
        pid = self.pid()
        pr = gitea_pr(101, comments=[comment('Decision: Approved for manual merge', 6)])
        store.apply_snapshot(self.db, pid, self.snap(prs=[pr]))
        self._handler_runtime(pid, 'BLOCKED', 4)
        verdict, at, _ = store.latest_verdict(self.db, pid, 101)
        self.assertEqual('Approved for manual merge', verdict)
        self.assertEqual(epoch(6), at)


class QueueTests(StoreTestCase):
    def load(self, issues=(), prs=()):
        pid = self.pid()
        store.apply_snapshot(self.db, pid,
                             {'issues': list(issues), 'prs': list(prs), 'base_sha': 'b'})
        return pid

    def test_pending_merges(self):
        good = gitea_pr(101, comments=[comment('Approved for manual merge', 3)])
        rejected = gitea_pr(102, comments=[comment('Changes requested', 3)])
        merged = gitea_pr(103, state='closed', merged=True, merged_at=ts(4),
                          comments=[comment('Approved for manual merge', 3)])
        self.load(prs=[good, rejected, merged])
        rows = store.pending_merges(self.db)
        self.assertEqual([101], [r['number'] for r in rows])
        self.assertEqual('a__b', rows[0]['org_repo'])
        aged = store.pending_merges(self.db, now=self.now)
        self.assertAlmostEqual(self.now - epoch(3), aged[0]['waiting_seconds'], delta=1)

    def test_pending_decisions_by_title_and_label(self):
        tagged = gitea_issue(1, title='[❓needs-human] pick one', updated_at=ts(5))
        labeled = gitea_issue(2, labels=[{'name': 'needs-human'}])
        plain = gitea_issue(3)
        closed = gitea_issue(4, state='closed', title='[❓needs-human] old')
        self.load(issues=[tagged, labeled, plain, closed])
        rows = store.pending_decisions(self.db)
        self.assertEqual({1, 2}, {r['number'] for r in rows})

    def test_pending_decisions_reply_removes(self):
        """User reply via dashboard (recorded in user_replies) newer than the
        last comment -> issue leaves the queue immediately."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单 🙋** 请拍板', 3)])])
        store.record_user_reply(self.db, pid, 1, '1A 本期做', ts(6))
        rows = store.pending_decisions(self.db)
        self.assertEqual([], [r['number'] for r in rows])

    def test_pending_decisions_reply_then_clarification_returns(self):
        """Handler clarification comment after the user reply -> back in queue."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单 🙋** 1. 选型', 3),
                                                      comment('1A 本期做', 6, poster='mocx'),
                                                      agent_comment('请澄清', 7)])])
        store.record_user_reply(self.db, pid, 1, '1A 本期', ts(6))
        rows = store.pending_decisions(self.db)
        self.assertEqual([1], [r['number'] for r in rows])

    def test_pending_decisions_reply_collected_back_equal_ts(self):
        """The reply comment collected back from Gitea has the same created_at
        as the recorded user_reply -> >= keeps it out of the queue."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单 🙋** 1. 选型', 3),
                                                      comment('1A', 6, poster='mocx')])])
        store.record_user_reply(self.db, pid, 1, '1A', ts(6))
        rows = store.pending_decisions(self.db)
        self.assertEqual([], [r['number'] for r in rows])

    def test_pending_decisions_reply_without_comments(self):
        """Tagged issue with no comments at all: a recorded reply exits it."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one')])
        store.record_user_reply(self.db, pid, 1, 'go', ts(5))
        rows = store.pending_decisions(self.db)
        self.assertEqual([], [r['number'] for r in rows])

    def test_pending_decisions_multiple_replies_latest_wins(self):
        """Only the newest reply matters: an older reply followed by a newer
        agent comment stays in queue."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单** 1. 选型', 3),
                                                      agent_comment('请澄清', 6)])])
        store.record_user_reply(self.db, pid, 1, '1A', ts(5))
        rows = store.pending_decisions(self.db)
        self.assertEqual([1], [r['number'] for r in rows])

    def test_pending_decisions_unreplied_stays(self):
        """No user reply recorded -> unchanged behavior."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                        comments=[agent_comment('**决策清单 🙋** 1. 选型', 3)])])
        rows = store.pending_decisions(self.db)
        self.assertEqual([1], [r['number'] for r in rows])

    def test_pending_decisions_empty_reply_ts_no_crash(self):
        """A user_reply row with an empty/unparseable created_at (Gitea receipt
        missing the field) must not crash the derived view."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                        comments=[agent_comment('**决策清单 🙋** 1. 选型', 3)])])
        store.record_user_reply(self.db, pid, 1, '1A', '')
        rows = store.pending_decisions(self.db)
        self.assertEqual([1], [r['number'] for r in rows])

    def test_pending_decisions_direct_gitea_reply_removes(self):
        """A human replying directly on Gitea (no agent marker, same account)
        must dequeue the issue even without a dashboard proxy reply."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单 🙋** 1. 选型', 3),
                                                      comment('1A 本期做', 6, poster='mocx')])])
        rows = store.pending_decisions(self.db)
        self.assertEqual([], [r['number'] for r in rows])

    def test_pending_decisions_agent_comment_not_user_reply(self):
        """An agent comment (with marker) after the decision list is not a user
        reply: the issue stays in queue."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单 🙋** 1. 选型', 3),
                                                      agent_comment('补充证据', 6)])])
        rows = store.pending_decisions(self.db)
        self.assertEqual([1], [r['number'] for r in rows])

    def test_pending_decisions_agent_then_direct_reply_removes(self):
        """Agent clarification followed by a direct human reply dequeues."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[agent_comment('**决策清单 🙋** 1. 选型', 3),
                                                      agent_comment('请澄清', 5),
                                                      comment('2B 本期做', 6, poster='mocx')])])
        rows = store.pending_decisions(self.db)
        self.assertEqual([], [r['number'] for r in rows])

    def test_pending_decisions_legacy_agent_comment_not_user_reply(self):
        """Pre-migration agent comments carry no new marker: a legacy decision
        list (anchor only) or a legacy opcode-marked comment must not be
        mistaken for a user reply, or every backlog issue would silently leave
        the queue on upgrade."""
        pid = self.load(issues=[gitea_issue(1, title='[❓needs-human] pick one',
                                            comments=[comment('**决策清单 🙋** 1. 选型', 3)])])
        rows = store.pending_decisions(self.db)
        self.assertEqual([1], [r['number'] for r in rows])
        pid2 = self.load(issues=[gitea_issue(2, title='[❓needs-human] pick two',
                                             comments=[comment('<!-- code-loop:legacy-op --> 决策清单', 3)])])
        rows = store.pending_decisions(self.db)
        self.assertEqual({1, 2}, {r['number'] for r in rows})

    def test_exceptions_outbox_and_lease_and_pr(self):
        pid = self.load(prs=[gitea_pr(101, comments=[comment('Blocked', 3)])])
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'events': [],
            'leases': [{'resource': 'wt1', 'execution_id': 1, 'token': '',
                        'expires_at': ts(2)}],
            'outbox': [{'id': 5, 'execution_id': 1, 'action': 'comment_issue',
                        'state': 'UNKNOWN', 'receipt': None}]})
        items = store.exceptions(self.db, now=self.now)
        kinds = {i['type'] for i in items}
        self.assertIn('outbox', kinds)
        self.assertIn('lease_expired', kinds)
        self.assertIn('pr', kinds)

    def test_outbox_text_cp_id_does_not_raise(self):
        """The CP runtime outbox id is a 64-hex operation id, stored as TEXT
        despite the INTEGER column affinity (SQLite dynamic typing). A row in
        SENDING/UNKNOWN must surface as a card/exception, not crash the
        whole kanban/queue endpoints with '%d format: a real number is
        required' (2026-09-13 production incident)."""
        pid = self.load()
        hex_id = '37141ee8277e4c25ca8fd6554a1a2c94bec8457459284d4c65f4b6f4d551e422'
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'events': [], 'leases': [],
            'outbox': [{'id': hex_id, 'execution_id': None,
                        'action': 'create_issue', 'state': 'SENDING',
                        'receipt': None}]})
        board = store.kanban(self.db, now=self.now)
        card = next(c for c in board['blocked'] if c['card'] == 'outbox')
        self.assertIn(hex_id, card['title'])
        items = store.exceptions(self.db, now=self.now)
        outbox = next(i for i in items if i['type'] == 'outbox')
        self.assertEqual('outbox-' + hex_id, outbox['key'])
        self.assertIn(hex_id, outbox['title'])


class IssueDetailTests(StoreTestCase):
    def load(self, issues=(), prs=()):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {
            'issues': list(issues), 'prs': list(prs), 'base_sha': 'b'})
        return pid

    def test_issue_detail_full_thread_ascending(self):
        """Detail view data: full body plus every comment, oldest first —
        feed order must not matter."""
        self.load(issues=[gitea_issue(7, body='## 全文正文', comments=[
            comment('third', 4),
            comment('first', 2),
            comment('second', 3, hour=9),
            comment('same-day-later', 3, hour=10),
        ])])
        detail = store.issue_detail(self.db, 'a__b', 7)
        self.assertIsNotNone(detail)
        self.assertEqual(detail['number'], 7)
        self.assertEqual(detail['title'], 'Issue 7')
        self.assertEqual(detail['body'], '## 全文正文')
        self.assertEqual([c['body'] for c in detail['comments']],
                         ['first', 'second', 'same-day-later', 'third'])
        self.assertTrue(all({'poster', 'body', 'created_at'} <= set(c)
                            for c in detail['comments']))

    def test_issue_detail_unknown_project(self):
        self.load(issues=[gitea_issue(7)])
        self.assertIsNone(store.issue_detail(self.db, 'other__x', 7))

    def test_issue_detail_unknown_number(self):
        self.load(issues=[gitea_issue(7)])
        self.assertIsNone(store.issue_detail(self.db, 'a__b', 8))


class FunnelTrendTests(StoreTestCase):
    def test_funnel_counts(self):
        pid = self.pid()
        issues = [gitea_issue(1), gitea_issue(2)]
        prs = [gitea_pr(101, body='Closes #1'),
               gitea_pr(102, body='Closes #2', comments=[comment('Changes requested', 3)]),
               gitea_pr(103, body='Closes #3', comments=[comment('Approved for manual merge', 3)])]
        store.apply_snapshot(self.db, pid, {'issues': issues, 'prs': prs, 'base_sha': 'b'})
        f = store.funnel(self.db, pid)
        self.assertEqual(2, f['open_issues'])
        self.assertEqual(2, f['in_development'])
        self.assertEqual(1, f['awaiting_review'])
        self.assertEqual(1, f['changes_requested'])
        self.assertEqual(1, f['ready_merge'])

    def test_trends_bucket_local_timezone(self):
        # 2026-09-04T20:00Z = 2026-09-05 04:00 local (UTC+8): bucket by local day.
        pid = self.pid()
        issue = gitea_issue(1, created_at=ts(4, 20))
        store.apply_snapshot(self.db, pid, self.snap(issues=[issue]))
        t = store.trends(self.db, days=10, now=epoch(10))
        daily = {d['date']: d for d in t['daily']}
        self.assertEqual(1, daily['2026-09-05']['issues_created'])
        self.assertEqual(0, daily['2026-09-04']['issues_created'])

    def test_trends_daily_and_cycle(self):
        pid = self.pid()
        issue = gitea_issue(1, created_at=ts(3, 1))
        pr = gitea_pr(101, body='Closes #1', created_at=ts(4, 2),
                      comments=[comment('Approved for manual merge', 5, 3)])
        closed = gitea_issue(2, state='closed', created_at=ts(5), closed_at=ts(6))
        store.apply_snapshot(self.db, pid,
                             {'issues': [issue, closed], 'prs': [pr], 'base_sha': 'b'})
        t = store.trends(self.db, days=10, now=epoch(10))
        daily = {d['date']: d for d in t['daily']}
        self.assertEqual(1, daily['2026-09-03']['issues_created'])
        self.assertEqual(1, daily['2026-09-06']['issues_closed'])
        self.assertEqual(1, daily['2026-09-04']['prs_created'])
        # issue#1 created 09-03T01 -> pr created 09-04T02 = 25h; pr -> approved = ~25h
        issue_cycle = t['cycle']['issue_to_pr']
        self.assertEqual(1, issue_cycle['n'])
        self.assertAlmostEqual(25.0, issue_cycle['p50'], delta=0.1)
        self.assertAlmostEqual(25.0, t['cycle']['pr_to_approved']['p50'], delta=0.1)


class LoopAlertTests(StoreTestCase):
    def test_parse_ts_accepts_epoch_and_iso(self):
        self.assertAlmostEqual(1788854889.87, store.parse_ts('1788854889.87'), places=2)
        self.assertAlmostEqual(1788854889.0, store.parse_ts(1788854889), places=3)
        self.assertEqual(epoch(1), store.parse_ts(ts(1)))
        self.assertIsNone(store.parse_ts('not-a-date'))
        self.assertIsNone(store.parse_ts(''))

    def runtime(self, kind, last_day, last_hour=0):
        return {
            'work_items': [{'id': 1, 'key': 'k', 'kind': kind, 'state': 'DONE',
                            'priority': None, 'created_at': ts(last_day, last_hour),
                            'payload': '{}', 'version': 1, 'resources': '{}'}],
            'executions': [{'id': 1, 'item_id': 1, 'worker': 'w', 'state': 'FINISHED',
                            'result': None, 'worktree': '', 'branch': '', 'head': '',
                            'expires_at': None, 'created_at': ts(last_day, last_hour)}],
            'leases': [], 'outbox': [], 'events': []}

    def test_replace_runtime_tolerates_nulls(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, {
            'work_items': [{'id': 1, 'key': None, 'kind': 'discover', 'state': None,
                            'priority': None, 'version': None, 'payload': '{}',
                            'resources': None, 'created_at': ts(2)}],
            'executions': [{'id': 2, 'item_id': 1, 'worker': 'w', 'state': 'RUNNING',
                            'result': None, 'worktree': None, 'branch': None,
                            'head': None, 'expires_at': None, 'created_at': ts(2)}],
            'leases': [], 'outbox': [], 'events': []})
        row = db_row = self.db.execute('SELECT * FROM executions').fetchone()
        self.assertIsNone(row['worktree'])
        self.assertEqual('RUNNING', row['state'])

    def test_replace_runtime_identical_snapshot_has_no_mirror_dml(self):
        pid = self.pid()
        runtime = {
            'work_items': [{'id': 7, 'key': 'issue:7', 'kind': 'handler',
                            'state': 'READY', 'priority': 2, 'version': 1,
                            'payload': {'title': 'ship'}, 'resources': ['cpu'],
                            'created_at': '2026-10-20T00:00:00Z'}],
            'executions': [{'id': 8, 'item_id': 7, 'worker': 'box/handler',
                            'state': 'RUNNING', 'result': None, 'worktree': 'wt',
                            'branch': 'main', 'head': 'abc', 'expires_at': None,
                            'created_at': '2026-10-20T00:01:00Z'}],
            'leases': [{'resource': 'project/a', 'execution_id': 8,
                        'token': 'first-token', 'expires_at': 'later'}],
            'outbox': [{'id': 9, 'execution_id': 8, 'action': 'comment',
                        'state': 'SENT', 'receipt': '{}'}],
            'cp_events': [{'id': 10, 'kind': 'claim', 'payload': '{}',
                           'created_at': '2026-10-20T00:02:00Z'}],
            'commands': [{'id': 11, 'action': 'review_round', 'number': 7,
                          'state': 'RUNNING', 'execution': 'e1', 'agent': 'a',
                          'model': 'm', 'created_at': 1.0}],
        }
        store.replace_runtime(self.db, pid, runtime)
        ids = {table: self.db.execute('SELECT id FROM %s WHERE project_id=?' % table,
                                      (pid,)).fetchone()[0]
               for table in ('work_items', 'executions', 'outbox', 'cp_events', 'commands')}
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            store.replace_runtime(self.db, pid, runtime)
        finally:
            self.db.set_trace_callback(None)
        mirror_dml = [sql for sql in statements if sql.lstrip().upper().startswith(
            ('INSERT', 'UPDATE', 'DELETE')) and any(
                table in sql.lower() for table in
                ('work_items', 'executions', 'leases', 'outbox', 'cp_events', 'commands'))]
        self.assertEqual([], mirror_dml)
        for table, row_id in ids.items():
            self.assertEqual(row_id, self.db.execute(
                'SELECT id FROM %s WHERE project_id=?' % table, (pid,)).fetchone()[0])

    def test_replace_runtime_updates_lease_content_in_place(self):
        pid = self.pid()
        first = {'work_items': [], 'executions': [],
                 'leases': [{'resource': 'project/a', 'execution_id': 8,
                             'token': 'old', 'expires_at': 'soon'}],
                 'outbox': [], 'events': [], 'commands': []}
        store.replace_runtime(self.db, pid, first)
        before = self.db.execute('SELECT id FROM leases WHERE project_id=?',
                                 (pid,)).fetchone()['id']
        second = dict(first, leases=[dict(first['leases'][0], token='rotated')])
        store.replace_runtime(self.db, pid, second)
        row = self.db.execute('SELECT id, token FROM leases WHERE project_id=?',
                              (pid,)).fetchone()
        self.assertEqual((before, 'rotated'), (row['id'], row['token']))

    def test_replace_runtime_handles_text_ids_and_collapses_legacy_leases(self):
        pid = self.pid()
        self.db.execute("INSERT INTO leases(project_id,resource,execution_cp_id,token) "
                        "VALUES (?,?,?,?)", (pid, 'r1', 2, 'old'))
        self.db.execute("INSERT INTO leases(project_id,resource,execution_cp_id,token) "
                        "VALUES (?,?,?,?)", (pid, 'r1', 2, 'old'))
        runtime = {
            'work_items': [{'id': '007', 'key': 'k', 'kind': 'handler',
                            'state': 'READY', 'payload': '{}'},
                           {'id': '9007199254740992', 'key': 'large-a',
                            'kind': 'handler', 'state': 'READY', 'payload': '{}'},
                           {'id': '9007199254740993', 'key': 'large-b',
                            'kind': 'handler', 'state': 'READY', 'payload': '{}'}],
            'executions': [{'id': 'execution-x', 'item_id': '007',
                            'state': 'RUNNING'}],
            'leases': [{'resource': 'r1', 'execution_id': 2,
                        'token': 'new', 'expires_at': None},
                       {'resource': '7', 'execution_id': 3,
                        'token': 'seven', 'expires_at': None},
                       {'resource': '007', 'execution_id': 4,
                        'token': 'padded-seven', 'expires_at': None}],
            'outbox': [], 'cp_events': [], 'commands': [],
        }
        store.replace_runtime(self.db, pid, runtime)
        self.assertEqual(7, self.db.execute(
            'SELECT cp_id FROM work_items WHERE project_id=?', (pid,)).fetchone()[0])
        self.assertEqual([7, 9007199254740992, 9007199254740993], [row[0] for row in
            self.db.execute('SELECT cp_id FROM work_items WHERE project_id=? ORDER BY id',
                            (pid,))])
        self.assertEqual('execution-x', self.db.execute(
            'SELECT cp_id FROM executions WHERE project_id=?', (pid,)).fetchone()[0])
        self.assertEqual(3, self.db.execute(
            'SELECT COUNT(*) FROM leases WHERE project_id=?', (pid,)).fetchone()[0])
        tokens = {row['resource']: row['token'] for row in self.db.execute(
            'SELECT resource,token FROM leases WHERE project_id=?', (pid,))}
        self.assertEqual({'r1': 'new', '7': 'seven', '007': 'padded-seven'}, tokens)

    def test_runtime_numeric_identity_is_exact_and_unstorable_ids_fail_atomically(self):
        first = '12345678901234567890123456789012'
        second = '12345678901234567890123456789013'
        with localcontext() as context:
            context.prec = 4
            self.assertNotEqual(_runtime_identity_key(first),
                                _runtime_identity_key(second))

        pid = self.pid()
        with self.assertRaisesRegex(ValueError, 'outside SQLite INTEGER range'):
            store.replace_runtime(self.db, pid, {
                'work_items': [{'id': first, 'key': 'oversized', 'kind': 'handler',
                                'state': 'READY'}],
                'executions': [], 'leases': [], 'outbox': [], 'events': [],
                'commands': [],
            })
        self.assertEqual(0, self.db.execute(
            'SELECT COUNT(*) FROM work_items WHERE project_id=?', (pid,)).fetchone()[0])

    def test_replace_runtime_only_writes_changed_identities(self):
        pid = self.pid()
        original = {
            'work_items': [{'id': 1, 'key': 'k', 'kind': 'handler', 'state': 'READY',
                            'priority': 1, 'version': 1, 'payload': '{}',
                            'resources': '{}', 'created_at': 't1'}],
            'executions': [],
            'leases': [{'resource': 'r1', 'execution_id': 1,
                        'token': 't1', 'expires_at': None}],
            'outbox': [], 'cp_events': [], 'commands': [],
        }
        store.replace_runtime(self.db, pid, original)
        changed = dict(original,
                       work_items=[dict(original['work_items'][0], state='RUNNING')],
                       leases=[])
        statements = []
        self.db.set_trace_callback(statements.append)
        try:
            store.replace_runtime(self.db, pid, changed)
        finally:
            self.db.set_trace_callback(None)
        dml = [sql.lower() for sql in statements if sql.lstrip().upper().startswith(
            ('INSERT', 'UPDATE', 'DELETE')) and any(
                table in sql.lower() for table in
                ('work_items', 'executions', 'leases', 'outbox', 'cp_events', 'commands'))]
        self.assertEqual(2, len(dml))
        self.assertTrue(any('update work_items' in sql for sql in dml))
        self.assertTrue(any('delete from leases' in sql for sql in dml))

    def test_replace_runtime_preserves_nested_transaction_ownership(self):
        pid = self.pid()
        runtime = self.runtime('handler', 19)
        self.db.execute('BEGIN')
        self.db.execute("UPDATE projects SET base='caller-owned' WHERE id=?", (pid,))
        store.replace_runtime(self.db, pid, runtime)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual('', self.db.execute(
            'SELECT base FROM projects WHERE id=?', (pid,)).fetchone()[0])
        self.assertEqual(0, self.db.execute(
            'SELECT COUNT(*) FROM work_items WHERE project_id=?', (pid,)).fetchone()[0])

    def test_replace_runtime_rolls_back_all_tables_on_mid_project_failure(self):
        pid = self.pid()
        original = self.runtime('handler', 19)
        original['commands'] = []
        original['leases'] = [{'resource': 'project/a', 'execution_id': 1,
                               'token': 't1', 'expires_at': None}]
        store.replace_runtime(self.db, pid, original)
        self.db.execute("CREATE TRIGGER fail_execution_update BEFORE UPDATE ON executions "
                        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        changed = self.runtime('reviewer', 20)
        changed['commands'] = []
        changed['leases'] = []
        with self.assertRaises(sqlite3.IntegrityError):
            store.replace_runtime(self.db, pid, changed)
        self.assertEqual('handler', self.db.execute(
            'SELECT kind FROM work_items WHERE project_id=?', (pid,)).fetchone()[0])
        self.assertEqual(1, self.db.execute(
            'SELECT COUNT(*) FROM leases WHERE project_id=?', (pid,)).fetchone()[0])
        self.assertEqual(1, self.db.execute(
            'SELECT COUNT(*) FROM executions WHERE project_id=?', (pid,)).fetchone()[0])

    def test_loop_health_reports_last_activity(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, self.runtime('discover', 19, 12))
        health = store.loop_health(self.db, now=self.now)
        self.assertEqual(1, len(health))
        row = health[0]
        self.assertEqual('discover', row['kind'])
        self.assertAlmostEqual(epoch(19, 12), row['last_activity'], delta=1)
        self.assertAlmostEqual(epoch(20, 12) - epoch(19, 12), row['age_seconds'], delta=1)

    def test_loop_health_with_epoch_timestamps(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, {
            'work_items': [{'id': 1, 'key': 'k', 'kind': 'handler', 'state': 'DONE',
                            'priority': None, 'version': None, 'payload': '{}',
                            'resources': None, 'created_at': '1788854889.87'}],
            'executions': [], 'leases': [], 'outbox': [], 'events': []})
        health = store.loop_health(self.db, now=1788860000.0)
        self.assertAlmostEqual(1788854889.87, health[0]['last_activity'], places=2)
        self.assertAlmostEqual(1788860000.0 - 1788854889.87, health[0]['age_seconds'], places=2)

    def test_stall_alert_for_old_loop(self):
        pid = self.pid()
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1)
        store.replace_runtime(self.db, pid, self.runtime('discover', 18))
        alerts = store.alerts(self.db, now=self.now, config={})
        rules = {(a['rule'], a['org_repo']) for a in alerts}
        self.assertIn(('loop_stall', 'a__b'), rules)

    def test_fresh_loop_no_stall(self):
        pid = self.pid()
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1)
        store.replace_runtime(self.db, pid, self.runtime('discover', 20, 10))
        alerts = store.alerts(self.db, now=self.now, config={})
        self.assertEqual([], [a for a in alerts if a['rule'] == 'loop_stall'])

    def test_alerts_carry_stable_entity_keys(self):
        pid = self.pid()
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1)
        store.apply_snapshot(self.db, pid, {
            'issues': [gitea_issue(7, title='[❓needs-human] old', created_at=ts(18),
                                   updated_at=ts(18))],
            'prs': [gitea_pr(101, comments=[comment('Blocked', 3)])], 'base_sha': 'b'})
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'leases': [], 'events': [],
            'outbox': [{'id': 5, 'execution_id': 1, 'action': 'comment_issue',
                        'state': 'UNKNOWN', 'receipt': None}]})
        alerts = store.alerts(self.db, now=self.now, config={})
        keys = {(a['rule'], a['key']) for a in alerts}
        self.assertIn(('needs_human_aging', 'issue-7'), keys)
        self.assertIn(('pr_blocked', 'pr-101'), keys)
        self.assertIn(('outbox_limbo', 'outbox-5'), keys)

    def test_machine_disabled_is_info(self):
        self.pid()
        alerts = store.alerts(self.db, now=self.now, config={})
        info = [a for a in alerts if a['rule'] == 'machine_disabled']
        self.assertEqual(1, len(info))
        self.assertEqual('info', info[0]['severity'])

    def test_needs_human_aging(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [
            gitea_issue(1, title='[❓needs-human] old', created_at=ts(18), updated_at=ts(18))],
            'prs': [], 'base_sha': 'b'})
        alerts = store.alerts(self.db, now=self.now, config={})
        self.assertIn('needs_human_aging', {a['rule'] for a in alerts})

    def test_needs_human_aging_message_uses_days_over_48h(self):
        """issue #324：等人工决策时长 ≥48h 用「天」、<48h 用「小时」。
        376 小时 = 15.7 天，用「天」才符合直觉。"""
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [
            gitea_issue(1, title='[❓needs-human] old', created_at=ts(1), updated_at=ts(1)),
            gitea_issue(2, title='[❓needs-human] recent', created_at=ts(18), updated_at=ts(18))],
            'prs': [], 'base_sha': 'b'})
        alerts = store.alerts(self.db, now=self.now, config={})
        aging = {a['key']: a['message'] for a in alerts if a['rule'] == 'needs_human_aging'}
        # issue 1 等了 19 天（≥48h）→ 天；issue 2 等了 2 天（≥48h）→ 天
        self.assertEqual(2, len(aging))
        self.assertTrue(all('天' in m for m in aging.values()))
        self.assertFalse(any('小时' in m for m in aging.values()))

    def test_collect_stale(self):
        self.pid()
        self.db.execute('UPDATE projects SET last_ok_collect_at=?', (self.now - 90000,))
        alerts = store.alerts(self.db, now=self.now, config={})
        self.assertIn('collect_stale', {a['rule'] for a in alerts})


class ReviewFixTests(StoreTestCase):
    """2026-09-09 review round: bug fixes + closed-loop data for the UI."""

    def test_loop_health_full_matrix_for_enabled_projects(self):
        pid = self.pid()
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1)
        health = store.loop_health(self.db, now=self.now)
        self.assertEqual(['discover', 'handler', 'reviewer'],
                         sorted(r['kind'] for r in health))
        self.assertEqual({None}, {r['last_activity'] for r in health})
        self.assertEqual({0}, {r['running'] for r in health})

    def runtime(self, kind, last_day, last_hour=0):
        return {
            'work_items': [{'id': 1, 'key': 'k', 'kind': kind, 'state': 'DONE',
                            'priority': None, 'created_at': ts(last_day, last_hour),
                            'payload': '{}', 'version': 1, 'resources': '{}'}],
            'executions': [{'id': 1, 'item_id': 1, 'worker': 'w', 'state': 'FINISHED',
                            'result': None, 'worktree': '', 'branch': '', 'head': '',
                            'expires_at': None, 'created_at': ts(last_day, last_hour)}],
            'leases': [], 'outbox': [], 'events': []}

    def test_loop_health_partial_matrix_merges_facts(self):
        pid = self.pid()
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1)
        store.replace_runtime(self.db, pid, self.runtime('discover', 19, 12))
        health = store.loop_health(self.db, now=self.now)
        by_kind = {r['kind']: r for r in health}
        self.assertEqual(3, len(health))
        self.assertAlmostEqual(epoch(19, 12), by_kind['discover']['last_activity'], delta=1)
        self.assertIsNone(by_kind['handler']['last_activity'])

    def test_pending_decisions_age_from_creation(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [
            gitea_issue(1, title='[❓needs-human] old batch', created_at=ts(5),
                        updated_at=ts(19, 12))], 'prs': [], 'base_sha': 'b'})
        row = store.pending_decisions(self.db, now=self.now)[0]
        self.assertEqual('created_at', row['age_basis'])
        self.assertAlmostEqual(self.now - epoch(5), row['waiting_seconds'], delta=1)

    def test_exceptions_lease_carries_execution_id(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'events': [],
            'leases': [{'resource': 'issue-discover', 'execution_id': 33, 'token': 't',
                        'expires_at': '1788874948.0987'}],
            'outbox': []})
        items = store.exceptions(self.db, now=1788880000.0)
        lease = [i for i in items if i['type'] == 'lease_expired'][0]
        self.assertEqual(33, lease['execution_cp_id'])

    def test_exceptions_pr_carries_verdict_body(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [], 'prs': [gitea_pr(101, comments=[
            comment('## Reviewer 评审结论：Blocked\n\n合并到 dev 存在冲突，本轮停止', 3)])],
            'base_sha': 'b'})
        items = store.exceptions(self.db, now=self.now)
        pr_item = [i for i in items if i['type'] == 'pr'][0]
        self.assertIn('合并到 dev 存在冲突', pr_item['verdict_body'])

    def test_prs_store_mergeable(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [], 'prs': [
            gitea_pr(101, mergeable=False), gitea_pr(102, mergeable=True),
            gitea_pr(103)], 'base_sha': 'b'})
        rows = {r['number']: r['mergeable'] for r in
                self.db.execute('SELECT number, mergeable FROM prs')}
        self.assertEqual({101: 0, 102: 1, 103: None}, rows)
        # flows through to pending_merges/_pr_rows consumers
        self.pid()
        for row in self.db.execute('SELECT mergeable FROM prs'):
            self.assertIn(row['mergeable'], (0, 1, None))

    def test_trends_project_filter(self):
        pid_a = store.upsert_project(self.db, 'a__b', 'cp1.json')
        pid_b = store.upsert_project(self.db, 'c__d', 'cp2.json')
        store.apply_snapshot(self.db, pid_a, {'issues': [
            gitea_issue(1, created_at=ts(3))], 'prs': [], 'base_sha': 'b'})
        store.apply_snapshot(self.db, pid_b, {'issues': [
            gitea_issue(1, created_at=ts(4))], 'prs': [], 'base_sha': 'b'})
        everything = store.trends(self.db, days=10, now=epoch(10))
        only_a = store.trends(self.db, days=10, now=epoch(10), org_repo='a__b')
        self.assertEqual(2, sum(d['issues_created'] for d in everything['daily']))
        self.assertEqual(1, sum(d['issues_created'] for d in only_a['daily']))

    def test_events_grouped_by_day(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'leases': [], 'outbox': [],
            'cp_events': [
                {'id': 1, 'kind': 'execution.reported', 'payload': '{"decision":"CONFIRMED"}',
                 'created_at': ts(4, 12)},
                {'id': 2, 'kind': 'issue.published', 'payload': '{}',
                 'created_at': ts(5, 12)},
                {'id': 3, 'kind': 'old.event', 'payload': '{}',
                 'created_at': ts(3, 10)}]})
        # window = 7 local days ending epoch(10): covers 09-04/09-05, excludes 09-03
        data = store.events(self.db, days=7, now=epoch(10))
        self.assertEqual(2, data['total'])
        counts = {d['date']: d['count'] for d in data['daily']}
        self.assertEqual(1, counts['2026-09-04'])
        self.assertEqual(1, counts['2026-09-05'])
        self.assertNotIn('2026-09-03', counts)
        self.assertEqual({'execution.reported', 'issue.published'},
                         {e['kind'] for e in data['recent']})
        self.assertEqual('a__b', data['recent'][0]['org_repo'])

    def test_events_project_filter_unknown_is_empty(self):
        self.pid()
        data = store.events(self.db, org_repo='zz__zz', days=7, now=epoch(10))
        self.assertEqual(0, data['total'])

    def test_pr_carries_body_excerpt_for_decisions(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [
            gitea_issue(1, title='[❓needs-human] pick one', body='决策问题：选 A 还是 B？')],
            'prs': [], 'base_sha': 'b'})
        row = store.pending_decisions(self.db)[0]
        self.assertIn('决策问题', row['body'])


class VerdictHoistTests(StoreTestCase):
    """issue #130: _cp_verdicts rescans and JSON-parses all of a project's
    work_items x executions on every call. Derived views that loop over PRs
    must compute it once per project and pass the map down — 553 per-PR calls
    made /api/trends take 28-50s on a 22MB store."""

    def load_two_projects(self, n_prs=6):
        for i in range(2):
            pid = store.upsert_project(self.db, 'p%d__r%d' % (i, i), 'cp%d.json' % i)
            prs = [gitea_pr(100 + j,
                            **({'comments': [comment('Approved for manual merge', 3)]}
                               if j % 2 else {}))
                   for j in range(n_prs)]
            store.apply_snapshot(self.db, pid,
                                 {'issues': [gitea_issue(1)], 'prs': prs, 'base_sha': 'b'})

    def count_calls(self, fn, *args, **kw):
        calls = []
        original = verdicts._cp_verdicts

        def counted(db, pid):
            calls.append(pid)
            return original(db, pid)

        verdicts._cp_verdicts = counted
        try:
            return fn(*args, **kw), len(calls)
        finally:
            verdicts._cp_verdicts = original

    def test_trends_once_per_project(self):
        self.load_two_projects()
        _, n = self.count_calls(store.trends, self.db, days=10, now=epoch(10))
        self.assertEqual(2, n)  # one per project, not one per PR (12)

    def test_pending_merges_once_per_project(self):
        self.load_two_projects()
        _, n = self.count_calls(store.pending_merges, self.db, now=self.now)
        self.assertEqual(2, n)

    def test_exceptions_once_per_project(self):
        self.load_two_projects()
        _, n = self.count_calls(store.exceptions, self.db, now=self.now)
        self.assertEqual(2, n)

    def test_kanban_once_per_project(self):
        self.load_two_projects()
        _, n = self.count_calls(store.kanban, self.db, now=self.now)
        self.assertEqual(2, n)

    def test_funnel_once_per_project(self):
        pid = store.upsert_project(self.db, 'p0__r0', 'cp.json')
        store.apply_snapshot(self.db, pid, {
            'issues': [gitea_issue(1)],
            'prs': [gitea_pr(100 + j) for j in range(4)], 'base_sha': 'b'})
        _, n = self.count_calls(store.funnel, self.db, pid)
        self.assertEqual(1, n)

    def test_latest_verdict_with_precomputed_map_skips_scan(self):
        pid = store.upsert_project(self.db, 'p0__r0', 'cp.json')
        store.apply_snapshot(self.db, pid, {
            'issues': [],
            'prs': [gitea_pr(101, comments=[comment('Approved for manual merge', 3)])],
            'base_sha': 'b'})
        calls = []
        original = verdicts._cp_verdicts

        def counted(db, p):
            calls.append(p)
            return original(db, p)

        verdicts._cp_verdicts = counted
        try:
            verdict = store.latest_verdict(
                self.db, pid, 101,
                cp_verdicts={101: ('Approved for manual merge', epoch(3), 'rev', 'ok')})
        finally:
            verdicts._cp_verdicts = original
        self.assertEqual(('Approved for manual merge', epoch(3), 'rev'), verdict)
        self.assertEqual([], calls)  # the supplied map is authoritative, no rescan

class TriggerRegistrationTests(StoreTestCase):
    """set_project_trigger: per-project manual review-trigger registration
    (repo_cwd/entry_doc/rules_doc/spawn_command) on the projects table."""

    def test_register_creates_row_and_stores_all_fields(self):
        store.set_project_trigger(self.db, 'a__b', repo_cwd='D:/proj',
                                  entry_doc='D:/proj/e.md', rules_doc='D:/r.md',
                                  spawn_command=['claude', '-p'])
        row = self.db.execute('SELECT * FROM projects WHERE org_repo=?',
                              ('a__b',)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual('D:/proj', row['repo_cwd'])
        self.assertEqual('D:/proj/e.md', row['entry_doc'])
        self.assertEqual('D:/r.md', row['rules_doc'])
        self.assertEqual(['claude', '-p'], json.loads(row['spawn_command']))

    def test_register_edits_only_provided_keys(self):
        store.set_project_trigger(self.db, 'a__b', repo_cwd='D:/proj',
                                  entry_doc='D:/proj/e.md')
        store.set_project_trigger(self.db, 'a__b', rules_doc='D:/r.md')
        row = self.db.execute('SELECT * FROM projects WHERE org_repo=?',
                              ('a__b',)).fetchone()
        self.assertEqual('D:/proj', row['repo_cwd'])
        self.assertEqual('D:/proj/e.md', row['entry_doc'])
        self.assertEqual('D:/r.md', row['rules_doc'])

    def test_register_survives_collect_upsert(self):
        """collect's upsert_project must never clobber trigger columns."""
        store.set_project_trigger(self.db, 'a__b', repo_cwd='D:/proj')
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1)
        row = self.db.execute('SELECT * FROM projects WHERE org_repo=?',
                              ('a__b',)).fetchone()
        self.assertEqual('D:/proj', row['repo_cwd'])
        self.assertEqual('cp.json', row['config_path'])
        self.assertEqual(1, row['enabled'])

    def test_spawn_command_must_be_string_list(self):
        with self.assertRaises(ValueError):
            store.set_project_trigger(self.db, 'a__b', spawn_command='claude')
        with self.assertRaises(ValueError):
            store.set_project_trigger(self.db, 'a__b', spawn_command=[1, 2])
        with self.assertRaises(ValueError):
            store.set_project_trigger(self.db, 'a__b', spawn_command=['a', 2])

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            store.set_project_trigger(self.db, 'a__b', repo_cwd_x='y')

    def test_legacy_db_migration_adds_columns(self):
        """A store created before the trigger columns gains them on connect,
        with pre-existing rows intact."""
        legacy = Path(self.tmp.name) / 'legacy.sqlite'
        con = sqlite3.connect(str(legacy))
        con.execute("CREATE TABLE projects (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "org_repo TEXT NOT NULL UNIQUE, config_path TEXT NOT NULL DEFAULT '',"
                    "platform_url TEXT NOT NULL DEFAULT '', repo TEXT NOT NULL DEFAULT '',"
                    "base TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 0,"
                    "enabled_host TEXT NOT NULL DEFAULT '', enabled_at REAL,"
                    "last_collect_at REAL, last_ok_collect_at REAL, last_error TEXT)")
        con.execute("INSERT INTO projects (org_repo, config_path) VALUES ('old__p', 'c.json')")
        con.commit()
        con.close()
        db = store.connect(legacy)
        try:
            row = db.execute('SELECT * FROM projects').fetchone()
            for column in ('repo_cwd', 'entry_doc', 'rules_doc', 'spawn_command'):
                self.assertIn(column, row.keys())
            self.assertEqual('old__p', row['org_repo'])
            store.set_project_trigger(db, 'old__p', repo_cwd='D:/x')
            row = db.execute('SELECT repo_cwd FROM projects').fetchone()
            self.assertEqual('D:/x', row['repo_cwd'])
        finally:
            db.close()


class WorkerMonitorTests(StoreTestCase):
    """worker_summary: per-machine rollup over the execution mirror, 应动未动
    stall flags and the worker_stall alert."""

    def item(self, iid, kind, day, hour=0, key=None, state='DONE'):
        return {'id': iid, 'key': key or ('issue:%d' % iid), 'kind': kind,
                'state': state, 'priority': None, 'version': 1, 'payload': '{}',
                'resources': '{}', 'created_at': ts(day, hour)}

    def execution(self, eid, iid, worker, state, day, hour=0, expires=None,
                  created=None):
        return {'id': eid, 'item_id': iid, 'worker': worker, 'state': state,
                'result': None, 'worktree': '', 'branch': '', 'head': '',
                'expires_at': expires, 'created_at': created or ts(day, hour)}

    def runtime(self, execs, items):
        return {'work_items': items, 'executions': execs,
                'leases': [], 'outbox': [], 'events': []}

    def machines(self, *specs):
        """specs: (name, {enabled, revoked, last_seen, projects}) tuples."""
        rows = []
        for i, (name, extra) in enumerate(specs):
            rows.append({'id': 'm%d' % i, 'name': name,
                         'enabled': extra.get('enabled', 1),
                         'revoked': extra.get('revoked', 0), 'created_at': 0.0,
                         'last_seen': extra.get('last_seen'),
                         'last_addr': '10.0.0.%d' % i,
                         'agent_version': 'test',
                         'projects': extra.get('projects', 'a__b')})
        store.replace_machines(self.db, rows)

    def summary(self, **kw):
        return store.worker_summary(self.db, now=self.now, **kw)

    def by_name(self, summary, name):
        return next(m for m in summary['machines'] if m['name'] == name)

    def test_machine_is_stamped_worker_prefix(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 20, 10)],
            [self.item(1, 'handler', 20, 10)]))
        entry = self.by_name(self.summary(), 'box-a')
        self.assertFalse(entry['registered'])
        self.assertAlmostEqual(epoch(20, 10), entry['kinds']['handler']['last_claim'], delta=1)
        self.assertEqual(1, entry['kinds']['handler']['total'])

    def test_registry_machines_merge_with_activity(self):
        pid = self.pid()
        self.machines(('box-a', {'last_seen': epoch(20, 11), 'projects': 'a__b,c__d'}),
                      ('box-b', {}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/reviewer-1', 'SUCCEEDED', 20, 9)],
            [self.item(1, 'reviewer', 20, 9)]))
        summary = self.summary()
        active = self.by_name(summary, 'box-a')
        self.assertTrue(active['registered'])
        self.assertEqual('a__b,c__d', active['projects'])
        self.assertIn('reviewer', active['kinds'])
        idle = self.by_name(summary, 'box-b')
        self.assertTrue(idle['registered'])
        self.assertEqual({}, idle['kinds'])
        # 活跃注册机器排最前，空转注册机器随后
        self.assertEqual(['box-a', 'box-b'], [m['name'] for m in summary['machines']])

    def test_stalled_registered_machine_flags_and_alerts(self):
        pid = self.pid()
        self.machines(('box-a', {'last_seen': epoch(18)}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/discover-1', 'SUCCEEDED', 18)],
            [self.item(1, 'discover', 18)]))
        entry = self.by_name(self.summary(), 'box-a')
        self.assertEqual(['discover'], entry['stalled_kinds'])
        self.assertTrue(entry['kinds']['discover']['stalled'])
        alert = next(a for a in store.alerts(self.db, now=self.now, config={})
                     if a['rule'] == 'worker_stall')
        self.assertEqual('worker-box-a-discover', alert['key'])
        self.assertEqual('warn', alert['severity'])

    def test_stall_threshold_config_override(self):
        pid = self.pid()
        self.machines(('box-a', {}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/discover-1', 'SUCCEEDED', 18)],
            [self.item(1, 'discover', 18)]))
        config = {'worker_stall_seconds': {'discover': 10 ** 9}}
        entry = self.by_name(self.summary(config=config), 'box-a')
        self.assertFalse(entry['kinds']['discover']['stalled'])
        self.assertEqual([], [a for a in store.alerts(self.db, now=self.now,
                                                      config=config)
                              if a['rule'] == 'worker_stall'])

    def test_unregistered_worker_never_stalls(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'ghost/reviewer-1', 'SUCCEEDED', 1)],
            [self.item(1, 'reviewer', 1)]))
        entry = self.by_name(self.summary(), 'ghost')
        self.assertEqual([], entry['stalled_kinds'])
        self.assertEqual([], [a for a in store.alerts(self.db, now=self.now,
                                                      config={})
                              if a['rule'] == 'worker_stall'])

    def test_handler_silence_without_waiting_work_is_idle(self):
        """handler/reviewer leave no execution trace on an empty-queue round:
        silence without READY work must read as legitimate idle, not a stall."""
        pid = self.pid()
        self.machines(('box-a', {}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 18)],
            [self.item(1, 'handler', 18)]))
        entry = self.by_name(self.summary(), 'box-a')
        self.assertEqual([], entry['stalled_kinds'])
        self.assertFalse(entry['kinds']['handler']['stalled'])

    def test_handler_stall_requires_work_waiting_before_silence(self):
        pid = self.pid()
        self.machines(('box-a', {}))
        # READY 且创建于最后一次 claim 之前（ts(17) < ts(18)）→ 有活不取，判停摆
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 18)],
            [self.item(1, 'handler', 18),
             self.item(2, 'handler', 17, key='issue:2', state='READY')]))
        entry = self.by_name(self.summary(), 'box-a')
        self.assertEqual(['handler'], entry['stalled_kinds'])

    def test_handler_work_arriving_after_silence_is_not_a_stall(self):
        """READY item created after the machine's last claim: the next
        scheduled round will take it — silence so far is not yet a stall."""
        pid = self.pid()
        self.machines(('box-a', {}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 18)],
            [self.item(1, 'handler', 18),
             self.item(2, 'handler', 19, key='issue:2', state='READY')]))
        entry = self.by_name(self.summary(), 'box-a')
        self.assertEqual([], entry['stalled_kinds'])

    def test_handler_work_in_foreign_project_does_not_implicate_machine(self):
        """READY work in a project this machine never claimed must not flag it."""
        pid_a = self.pid()
        pid_other = store.upsert_project(self.db, 'c__d', 'cp2.json')
        self.machines(('box-a', {}))
        store.replace_runtime(self.db, pid_a, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 18)],
            [self.item(1, 'handler', 18)]))
        store.replace_runtime(self.db, pid_other, self.runtime(
            [],
            [self.item(2, 'handler', 17, key='issue:2', state='READY')]))
        entry = self.by_name(self.summary(), 'box-a')
        self.assertEqual([], entry['stalled_kinds'])

    def test_disabled_and_revoked_machines_never_stall(self):
        pid = self.pid()
        self.machines(('off-box', {'enabled': 0}), ('gone-box', {'revoked': 1}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'off-box/handler-1', 'SUCCEEDED', 1),
             self.execution(2, 2, 'gone-box/handler-1', 'SUCCEEDED', 1)],
            [self.item(1, 'handler', 1), self.item(2, 'handler', 1)]))
        summary = self.summary()
        self.assertEqual([], self.by_name(summary, 'off-box')['stalled_kinds'])
        self.assertEqual([], self.by_name(summary, 'gone-box')['stalled_kinds'])

    def test_running_executions_and_lease_expiry(self):
        pid = self.pid()
        self.machines(('box-a', {}))
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'RUNNING', 20, 10,
                            expires=epoch(20, 11)),
             self.execution(2, 2, 'box-a/handler-1', 'RUNNING', 20, 10,
                            expires=epoch(20, 13))],
            [self.item(1, 'handler', 20, 10, key='issue:16'),
             self.item(2, 'handler', 20, 10, key='issue:17')]))
        running = self.by_name(self.summary(), 'box-a')['running']
        self.assertEqual(2, len(running))
        expired = {r['key']: r['lease_expired'] for r in running}
        self.assertEqual({'issue:16': True, 'issue:17': False}, expired)
        self.assertEqual({'a__b'}, {r['org_repo'] for r in running})

    def test_outcome_window_counts_recent_only(self):
        pid = self.pid()
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 5),
             self.execution(2, 2, 'box-a/handler-1', 'FAILED', 15)],
            [self.item(1, 'handler', 5), self.item(2, 'handler', 15)]))
        entry = self.by_name(self.summary(), 'box-a')
        # ts(15) 距 now 5 天在 7 天窗口内；ts(5) 距 15 天在窗外
        self.assertEqual({'FAILED': 1}, entry['outcomes'])

    def test_mixed_timestamp_formats_recency(self):
        """ISO and epoch-text created_at mix in the mirror; recency must be
        parsed, not compared textually ('2026-…' always sorts above '1789…')."""
        pid = self.pid()
        store.replace_runtime(self.db, pid, self.runtime(
            [self.execution(1, 1, 'box-a/handler-1', 'SUCCEEDED', 20, 10),
             self.execution(2, 2, 'box-a/handler-2', 'SUCCEEDED', 20, 11,
                            created=str(epoch(20, 11)))],
            [self.item(1, 'handler', 20, 10), self.item(2, 'handler', 20, 11)]))
        slot = self.by_name(self.summary(), 'box-a')['kinds']['handler']
        self.assertAlmostEqual(epoch(20, 11), slot['last_claim'], delta=1)
        self.assertAlmostEqual(3600.0, slot['age_seconds'], delta=2)

    def test_empty_store_yields_empty_summary(self):
        summary = self.summary()
        self.assertEqual([], summary['machines'])
        self.assertEqual(7, summary['window_days'])


if __name__ == '__main__':
    unittest.main()
