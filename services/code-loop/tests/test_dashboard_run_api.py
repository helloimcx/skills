"""Run-detail endpoint + board deep-link tests (split from
test_dashboard_serve.py / test_dashboard_store.py to keep both under their
line-count ratchet baselines, issue #185 convention: split, don't raise).

GET /api/run serves the run-detail view: execution facts from the project's
live control-plane store (mirror as fallback), the bound session transcript
as flat events, the deterministic timeline. The board's reviewing card must
carry the CP execution id the deep link consumes."""
import json
import os
import sqlite3
import time
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parent),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.agent_runners import claude_transcript as runlog
from codeloop.agent_runners import opencode_storage
from codeloop.dash import serve, store

EXEC_ID = 'b0e7b8a1eaae4ccbaff45c3d61cf7e6f'


class RunApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = store.connect(self.root / 'dash.sqlite')
        cfg = self.root / 'cp.json'
        cfg.write_text('{}', encoding='utf-8')
        store.upsert_project(self.db, 'a__b', str(cfg))
        self.db.execute('UPDATE projects SET repo_cwd=? WHERE org_repo=?',
                        (str(self.root / 'repo'), 'a__b'))
        with self.db:
            pass
        # machine-local CP store: one RUNNING reviewer execution for review:184
        cp_db = self.root / 'state' / 'control-plane.sqlite'
        cp_db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(cp_db))
        con.executescript(
            'CREATE TABLE work_items (id TEXT PRIMARY KEY, key TEXT, kind TEXT, payload TEXT);'
            'CREATE TABLE executions (id TEXT PRIMARY KEY, item_id TEXT, worker TEXT,'
            ' token INTEGER, expires_at REAL, state TEXT, worktree TEXT, branch TEXT,'
            ' head TEXT, result TEXT, item_version TEXT, created_at REAL);'
            'CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT,'
            ' payload TEXT, created_at REAL);'
            'CREATE TABLE evidence (id TEXT PRIMARY KEY, execution_id TEXT, kind TEXT,'
            ' payload TEXT, created_at REAL);')
        con.execute('INSERT INTO work_items VALUES (?,?,?,?)',
                    ('item-1', 'review:184', 'reviewer',
                     json.dumps({'pr': {'number': 184, 'title': 'gzip 压缩',
                                        'html_url': 'http://g/pr/184'}})))
        con.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (EXEC_ID, 'item-1', 'h/reviewer', 212, self.epoch(600), 'RUNNING',
                     'D:/wt/review-pr-184', 'fix/mocx/dash-gzip', None, None, 'v1',
                     self.epoch(0)))
        con.execute('INSERT INTO events (kind, payload, created_at) VALUES (?,?,?)',
                    ('execution.derived', json.dumps({'id': EXEC_ID, 'state': 'RUNNING'}),
                     self.epoch(1)))
        con.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',
                    ('ev-1', EXEC_ID, 'verification', '{"ok": true}', self.epoch(2)))
        con.commit()
        con.close()
        # transcript root: a session whose prompt embeds the execution id
        self._old_root = runlog.PROJECTS_ROOT
        runlog.PROJECTS_ROOT = self.root / 'claude-projects'
        from test_claude_transcript import fixture_transcript
        self.transcript_dir = (runlog.PROJECTS_ROOT
                               / runlog.munged(str(self.root / 'repo')))
        self.transcript_dir.mkdir(parents=True)
        (self.transcript_dir / 'cbeb8c7e.jsonl').write_bytes(fixture_transcript(with_exec=True).encode('utf-8'))
        self.now = store.parse_ts('2026-09-08T00:00:00Z')
        handler = serve.create(self.db, now=lambda: self.now)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def epoch(self, offset):
        return store.parse_ts('2026-09-08T00:00:00Z') + offset

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        runlog.PROJECTS_ROOT = self._old_root
        self.tmp.cleanup()

    def get_json(self, path):
        request = Request(self.base + path)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8'))

    def test_run_happy_path_exact_binding(self):
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertEqual('RUNNING', data['execution']['state'])
        self.assertEqual('h/reviewer', data['execution']['worker'])
        self.assertEqual('review:184', data['item']['key'])
        self.assertEqual(184, data['item']['number'])
        self.assertEqual('gzip 压缩', data['item']['title'])
        self.assertEqual('exact', data['session']['bound'])
        self.assertTrue(data['session']['path'].endswith('cbeb8c7e.jsonl'))
        self.assertGreater(len(data['session']['events']), 0)
        self.assertIn('PR #184', data['session']['events'][0]['text'])
        self.assertEqual(['execution.derived'], [t['kind'] for t in data['timeline']])
        self.assertEqual(['verification'], [e['kind'] for e in data['evidence']])

    def test_run_incremental_since_returns_only_new_events(self):
        _, first = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        session = first['session']
        self.assertGreater(session['offset'], 0)
        status, second = self.get_json('/api/run?org=a__b&exec=%s&since=%d'
                                       % (EXEC_ID, session['offset']))
        self.assertEqual(200, status)
        self.assertEqual([], second['session']['events'])
        self.assertEqual(session['size'], second['session']['size'])

    def test_run_without_session_still_serves_the_timeline(self):
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertIsNone(data['session']['bound'])
        self.assertIsNone(data['session']['path'])
        self.assertEqual([], data['session']['events'])
        self.assertEqual(['execution.derived'], [t['kind'] for t in data['timeline']])

    def test_run_falls_back_to_mirror_when_live_store_missing(self):
        (self.root / 'state' / 'control-plane.sqlite').unlink()
        # mirror rows are CP-shaped: replace_runtime maps cp columns wholesale
        store.replace_runtime(self.db, 1, {
            'work_items': [{'id': 'item-1', 'key': 'review:184', 'kind': 'reviewer',
                            'state': 'READY', 'priority': None, 'version': 1,
                            'payload': json.dumps({'pr': {'number': 184,
                                                          'title': 'gzip 压缩'}}),
                            'resources': None, 'created_at': ''}],
            'executions': [{'id': EXEC_ID, 'item_id': 'item-1',
                            'worker': 'h/reviewer', 'state': 'RUNNING',
                            'result': None, 'worktree': 'D:/wt/review-pr-184',
                            'branch': 'fix/mocx/dash-gzip', 'head': None,
                            'expires_at': self.epoch(600),
                            'created_at': self.epoch(0)}],
            'leases': [], 'outbox': [],
            'cp_events': [{'id': 1, 'kind': 'execution.derived',
                           'payload': json.dumps({'id': EXEC_ID, 'state': 'RUNNING'}),
                           'created_at': self.epoch(1)}]})
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertEqual('RUNNING', data['execution']['state'])
        self.assertEqual('exact', data['session']['bound'])
        self.assertEqual(['execution.derived'], [t['kind'] for t in data['timeline']])
        self.assertEqual([], data['evidence'])   # evidence is live-store only

    def test_run_bypasses_the_snapshot_cache(self):
        """Spec Scope: the incremental polling endpoint skips the snapshot
        cache — no key churn, no stale-10s lag, shared last-good untouched."""
        handler = self.server.RequestHandlerClass
        status, _ = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertNotIn('/api/run?org=a__b&exec=%s' % EXEC_ID, handler.snapshot_cache)

    def test_run_rejects_unknown_org_and_bad_ids(self):
        self.assertEqual(404, self.get_json('/api/run?org=zz__zz&exec=%s' % EXEC_ID)[0])
        self.assertEqual(400, self.get_json('/api/run?org=a__b&exec=not-a-uuid')[0])
        other = 'f' * 32
        self.assertEqual(404, self.get_json('/api/run?org=a__b&exec=%s' % other)[0])
        self.assertEqual(400, self.get_json('/api/run?org=a__b&exec=%s&since=abc'
                                            % EXEC_ID)[0])

    def test_board_reviewing_card_carries_execution_id(self):
        """The deep link (#p=<org>&view=run&exec=<id>) consumes the CP
        execution id off the reviewing card — via /api/kanban, live store."""
        _, kb = self.get_json('/api/kanban?project=a__b')
        self.assertEqual(1, len(kb['reviewing']))
        self.assertEqual(EXEC_ID, kb['reviewing'][0]['execution'])

    def _add_progress_table(self, rows):
        """Add the agent-reported stream to the fixture CP store."""
        con = sqlite3.connect(str(self.root / 'state' / 'control-plane.sqlite'))
        con.executescript(
            'CREATE TABLE IF NOT EXISTS progress_events (execution TEXT NOT NULL,'
            ' seq INTEGER NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,'
            ' PRIMARY KEY (execution, seq));')
        for seq, kind, text in rows:
            con.execute('INSERT OR IGNORE INTO progress_events VALUES (?,?,?,?)',
                        (EXEC_ID, seq, json.dumps({'kind': kind, 'text': text}),
                         self.epoch(10 + seq)))
        con.commit()
        con.close()

    def test_run_reports_progress_stream_when_transcript_unbound(self):
        """Remote round: the transcript lives on the agent machine, so the
        local binding fails and the agent-reported stream takes over — same
        event shape, seq as the incremental cursor."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self._add_progress_table([(1, 'assistant', 'hi'), (2, 'tool', 'run')])
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertEqual('reported', data['session']['bound'])
        self.assertEqual(['hi', 'run'],
                         [e['text'] for e in data['session']['events']])
        self.assertEqual(2, data['session']['offset'])
        # incremental: since=<last seq> returns nothing new but the stream
        # still reports itself — exhausted is not "no stream" (issue #215):
        # bound stays 'reported' and size keeps the cursor, only events empty.
        status, again = self.get_json('/api/run?org=a__b&exec=%s&since=2' % EXEC_ID)
        self.assertEqual([], again['session']['events'])
        self.assertEqual('reported', again['session']['bound'])
        self.assertEqual(2, again['session']['size'])

    def test_run_with_table_but_no_stream_stays_unbound(self):
        """A store that has the progress table but no rows for this
        execution (historic round, transcript already gone): the unbound
        placeholder wins — an empty 'reported' session with its bind chip
        would be misleading (review MINOR/MAJOR follow-up)."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self._add_progress_table([])
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertIsNone(data['session']['bound'])
        self.assertEqual([], data['session']['events'])

    def test_run_progress_fallback_survives_a_missing_table(self):
        """Legacy store without the progress table: degrade to the unbound
        placeholder, never a 500."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertIsNone(data['session']['bound'])

    # ---- the round's own CLI log (opencode rounds write one per execution) ----
    def round_log(self, body, exec_id=None):
        """The per-execution spawn log the agent writes beside the CP store."""
        path = (self.root / 'state' / 'executions'
                / ((exec_id or EXEC_ID) + '.log'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body.encode('utf-8'))
        return path

    @staticmethod
    def opencode_stream():
        """A real `opencode run --format json` stream, condensed."""
        return ''.join(json.dumps(obj) + '\n' for obj in [
            {'type': 'step_start', 'timestamp': 1789723867426,
             'part': {'type': 'step-start'}},
            {'type': 'tool_use', 'timestamp': 1789723870491,
             'part': {'type': 'tool', 'tool': 'bash',
                      'state': {'status': 'completed',
                                'input': {'command': 'echo hi'},
                                'output': 'hi\n', 'metadata': {'exit': 0}}}},
            {'type': 'text', 'timestamp': 1789723909148,
             'part': {'type': 'text', 'text': 'done'}},
        ])

    def test_run_streams_the_round_log_when_no_transcript_binds(self):
        """An opencode round keeps no claude transcript: its CLI output is the
        stream, read from the same flat-event shape and resumed by byte
        offset."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.round_log(self.opencode_stream())
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertEqual('spawn-log', data['session']['bound'])
        self.assertTrue(data['session']['path'].endswith(EXEC_ID + '.log'))
        self.assertEqual(['$ echo hi', 'done'],
                         [e['text'] for e in data['session']['events']])
        offset = data['session']['offset']
        self.assertGreater(offset, 0)
        status, again = self.get_json('/api/run?org=a__b&exec=%s&since=%d'
                                      % (EXEC_ID, offset))
        self.assertEqual([], again['session']['events'])

    def test_empty_round_log_is_no_information_not_a_binding(self):
        """A round that died before writing a byte must not claim a binding:
        the timeline is the honest view (and the card carries the failure)."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.round_log('')
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual(200, status)
        self.assertIsNone(data['session']['bound'])
        self.assertEqual(['execution.derived'], [t['kind'] for t in data['timeline']])

    def test_bound_transcript_still_outranks_the_round_log(self):
        """claude rounds are unchanged: their transcript is richer than the
        CLI's own stdout, so it stays the first source."""
        self.round_log(self.opencode_stream())
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual('exact', data['session']['bound'])
        self.assertTrue(data['session']['path'].endswith('cbeb8c7e.jsonl'))

    def test_local_round_log_outranks_the_reported_stream(self):
        """When both exist the log wins: it is the round's own output, while
        the reported stream is the remote substitute (batched, capped)."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.round_log(self.opencode_stream())
        self._add_progress_table([(1, 'assistant', 'reported copy')])
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual('spawn-log', data['session']['bound'])
        self.assertEqual(['$ echo hi', 'done'],
                         [e['text'] for e in data['session']['events']])

    def test_round_log_of_another_execution_is_never_read(self):
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.round_log(self.opencode_stream(), exec_id='f' * 32)
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertIsNone(data['session']['bound'])

    def guessed_transcript(self, mtime_offset=5):
        """A recent transcript WITHOUT the execution id: the binder can only
        guess it by time window."""
        from test_claude_transcript import fixture_transcript
        path = self.transcript_dir / 'other-session.jsonl'
        path.write_bytes(fixture_transcript(with_exec=False).encode('utf-8'))
        stamp = self.epoch(mtime_offset)
        os.utime(path, (stamp, stamp))
        return path

    def test_a_guessed_transcript_never_outranks_the_round_log(self):
        """The window tier is a guess; the round log is keyed by execution id.
        A local opencode round must not show a neighbouring session's events."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.guessed_transcript()
        self.round_log(self.opencode_stream())
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual('spawn-log', data['session']['bound'])
        self.assertTrue(data['session']['path'].endswith(EXEC_ID + '.log'))
        self.assertEqual(['$ echo hi', 'done'],
                         [e['text'] for e in data['session']['events']])

    def test_the_guessed_transcript_still_serves_a_round_with_no_log(self):
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.guessed_transcript()
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertEqual('window', data['session']['bound'])
        self.assertTrue(data['session']['path'].endswith('other-session.jsonl'))

    def test_an_unreadable_round_log_is_not_a_binding(self):
        """A log whose first lines are not events (a CLI's plain error text)
        must not claim a binding: the timeline is the honest view."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        self.round_log('opencode: upstream 503\nstill not json\n')
        status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        self.assertIsNone(data['session']['bound'])
        self.assertEqual(['execution.derived'], [t['kind'] for t in data['timeline']])

    def _add_commands_table(self, rows):
        """Add the command ledger to the fixture CP store."""
        con = sqlite3.connect(str(self.root / 'state' / 'control-plane.sqlite'))
        con.executescript(
            'CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            ' action TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL,'
            ' target_machine TEXT, claimed_by TEXT, claimed_at REAL, spawned_at REAL,'
            ' attempts INTEGER NOT NULL DEFAULT 0, fail_reason TEXT, execution TEXT,'
            ' created_at REAL NOT NULL);')
        for number, state, reason in rows:
            con.execute(
                'INSERT INTO commands (action, number, state, fail_reason, created_at)'
                ' VALUES (?,?,?,?,?)',
                ('review_round', number, state, reason, self.epoch(5)))
        con.commit()
        con.close()

    def _seed_open_pr(self, number=184):
        """A collected open PR row so the board has a pr card to decorate."""
        with self.db:
            self.db.execute(
                'INSERT OR IGNORE INTO prs (project_id, number, state, title,'
                ' created_at) VALUES ((SELECT id FROM projects WHERE'
                " org_repo='a__b'), ?, 'open', ?, '')", (number, 'gzip'))

    def _clear_live_executions(self):
        """Drop the RUNNING reviewer execution so the pr card returns to the
        review column (a live execution owns the reviewing slot instead)."""
        con = sqlite3.connect(str(self.root / 'state' / 'control-plane.sqlite'))
        con.execute('DELETE FROM executions')
        con.commit()
        con.close()

    def test_board_review_card_carries_latest_trigger_state(self):
        self._seed_open_pr()
        self._clear_live_executions()
        self._add_commands_table([(184, 'FAILED', 'NOT_REGISTERED: x')])
        _, kb = self.get_json('/api/kanban?project=a__b')
        cards = [c for c in kb['review'] if c.get('number') == 184]
        self.assertEqual(1, len(cards))
        self.assertEqual({'state': 'FAILED', 'fail_reason': 'NOT_REGISTERED: x'},
                         {'state': cards[0]['trigger']['state'],
                          'fail_reason': cards[0]['trigger']['fail_reason']})

    def test_board_card_without_commands_carries_null_trigger(self):
        self._seed_open_pr()
        self._clear_live_executions()
        self._add_commands_table([])
        _, kb = self.get_json('/api/kanban?project=a__b')
        cards = [c for c in kb['review'] if c.get('number') == 184]
        self.assertEqual(1, len(cards))
        self.assertIsNone(cards[0]['trigger'])

    def test_preflight_endpoint_maps_to_wired_closure(self):
        handler = self.server.RequestHandlerClass
        handler.preflight = lambda keys: {'a__b': {'online': ['m1'],
                                                   'registered': ['m1']}}
        try:
            status, data = self.get_json('/api/review/preflight?projects=a__b')
        finally:
            del handler.preflight
        self.assertEqual(200, status)
        self.assertEqual({'online': ['m1'], 'registered': ['m1']}, data['a__b'])

    def test_board_reviewing_card_execution_from_mirror(self):
        """Mirror path: the card's execution id is the CP uuid (cp_id), not
        the mirror's local row id."""
        (self.root / 'state' / 'control-plane.sqlite').unlink()
        store.replace_runtime(self.db, 1, {
            'work_items': [{'id': 'item-1', 'key': 'review:184', 'kind': 'reviewer',
                            'state': 'READY', 'priority': None, 'version': 1,
                            'payload': json.dumps({'pr': {'number': 184}}),
                            'resources': None, 'created_at': ''}],
            'executions': [{'id': EXEC_ID, 'item_id': 'item-1',
                            'worker': 'h/reviewer', 'state': 'RUNNING',
                            'result': None, 'worktree': '', 'branch': '',
                            'head': None, 'expires_at': self.epoch(600),
                            'created_at': self.epoch(0)}],
            'leases': [], 'outbox': [], 'cp_events': []})
        _, kb = self.get_json('/api/kanban?project=a__b')
        self.assertEqual(1, len(kb['reviewing']))
        self.assertEqual(EXEC_ID, kb['reviewing'][0]['execution'])


    def test_run_reads_the_session_through_the_spawning_agent_plugin(self):
        """The command ledger names the agent a round ran under; the run view
        must read that plugin's output — an opencode round reads opencode's
        own storage, never a claude transcript the window tier could
        mis-guess (the pre-plugin ambiguity this dispatch removes)."""
        (self.transcript_dir / 'cbeb8c7e.jsonl').unlink()
        con = sqlite3.connect(str(self.root / 'state' / 'control-plane.sqlite'))
        con.executescript(
            'CREATE TABLE IF NOT EXISTS commands (id INTEGER PRIMARY KEY AUTOINCREMENT,'
            ' action TEXT NOT NULL, number INTEGER NOT NULL, state TEXT NOT NULL,'
            ' target_machine TEXT, claimed_by TEXT, claimed_at REAL, spawned_at REAL,'
            ' attempts INTEGER NOT NULL DEFAULT 0, fail_reason TEXT, execution TEXT,'
            ' agent TEXT, model TEXT, created_at REAL NOT NULL);')
        con.execute('INSERT INTO commands (action, number, state, execution,'
                    ' agent, created_at) VALUES (?,?,?,?,?,?)',
                    ('review_round', 184, 'SPAWNED', EXEC_ID, 'opencode',
                     self.epoch(5)))
        con.commit()
        con.close()
        old_storage = opencode_storage.STORAGE_ROOT
        opencode_storage.STORAGE_ROOT = self.root / 'opencode-storage'
        try:
            root = opencode_storage.STORAGE_ROOT
            for rel, data in [
                ('session/p1/ses_x.json',
                 {'id': 'ses_x', 'directory': str(self.root / 'repo'),
                  'time': {'created': 30000, 'updated': 30000}}),
                ('message/ses_x/msg_u.json',
                 {'id': 'msg_u', 'sessionID': 'ses_x', 'role': 'user',
                  'time': {'created': 30000}}),
                ('part/msg_u/prt_1.json',
                 {'id': 'prt_1', 'sessionID': 'ses_x', 'messageID': 'msg_u',
                  'type': 'text', 'text': '评审。execution=%s。' % EXEC_ID}),
            ]:
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(data), encoding='utf-8')
                mtime = self.epoch(30)
                os.utime(p, (mtime, mtime))
            status, data = self.get_json('/api/run?org=a__b&exec=%s' % EXEC_ID)
        finally:
            opencode_storage.STORAGE_ROOT = old_storage
        self.assertEqual(200, status)
        self.assertEqual('exact', data['session']['bound'])
        self.assertEqual('ses_x', data['session']['path'])


if __name__ == '__main__':
    unittest.main()
