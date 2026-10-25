#!/usr/bin/env python3
"""Worker agent progress reporting: the follower unit tests and the
end-to-end stream case (split out of test_cp_agent.py to stay under the
800-line red line; same transport-scripting conventions as there)."""
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop import agent as agent_mod
from codeloop.agent_runners import claude_transcript
from codeloop.cp.client import Client
from codeloop.cp.server import Coordinator, make_server
from codeloop.cp.worker import WorkerService
from test_cp_agent import FakeClient, registration


class ScriptedRunner:
    """A plugin's output face reduced to a script — one resolution and a
    tail iterator: exactly the surface follow_progress consumes."""

    def __init__(self, resolution, tails=()):
        self.resolution = resolution
        self.tails = iter(tails)

    def resolve_session(self, repo_cwd, exec_id, created_at):
        return self.resolution

    def tail(self, handle, offset=0, limit=None):
        return next(self.tails)


class ProgressFollowTests(unittest.TestCase):
    """The hello frame announces this machine's registrations at connect time;
    the progress follower tails the spawned session's output through the
    requested agent's plugin and reports flat events in idempotent batches —
    best effort, never round-affecting."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'repo').mkdir()
        self.client = FakeClient()
        self.agent = agent_mod.WorkerAgent(
            server_url='http://cp.example', token='tok', client=self.client,
            db_path=str(self.root / 'dash.sqlite'),
            registration_loader=lambda project: self.row)
        self.row = registration(self.root, 'claude')

    def tearDown(self):
        self.tmp.cleanup()

    def _register_row(self, org_repo, repo_cwd):
        from codeloop.dash import store as dash_store
        from codeloop.dash.ingest import set_project_trigger
        db = dash_store.connect(self.agent.db_path)
        try:
            set_project_trigger(db, org_repo, repo_cwd=repo_cwd)
        finally:
            db.close()

    def test_hello_frame_lists_registered_projects(self):
        self.assertEqual('hello', self.agent.hello_frame()['type'])
        self.assertEqual([1], self.agent.hello_frame()['task_schema_versions'])
        # nothing registered yet: an empty list, never a missing key
        self.assertEqual([], self.agent.hello_frame()['registrations'])
        self._register_row('a__b', str(self.root / 'repo'))
        # a collect-built row carries no repo_cwd: known but not registered
        from codeloop.dash import store as dash_store
        db = dash_store.connect(self.agent.db_path)
        try:
            db.execute("INSERT INTO projects (org_repo) VALUES ('c__d')")
            db.commit()
        finally:
            db.close()
        self.assertEqual(['a__b'],
                         self.agent.hello_frame()['registrations'])

    def test_follow_reports_batches_then_done_when_pid_exits(self):
        calls = []
        self.agent.report_progress = lambda project, execution, events, done=False:             calls.append((project, execution, events, done)) or len(events)
        resolution = {'bound': 'exact', 'path': 'T.jsonl'}
        tails = iter([
            {'events': [{'kind': 'assistant', 'text': 'a'},
                        {'kind': 'tool', 'name': 'Bash'},
                        {'kind': 'assistant', 'text': 'b'}], 'offset': 900},
            {'events': [], 'offset': 900},
        ])
        try:
            self.agent.follow_progress('a__b', 'e1', str(self.root / 'repo'),
                                       pid=4242, started_at=1000.0,
                                       runner=ScriptedRunner(resolution, tails),
                                       pid_alive=lambda p: False,
                                       clock=lambda: 1000.0,
                                       poll=0.0, idle_limit=1.0)
        finally:
            pass
        self.assertEqual(2, len(calls))
        first = calls[0]
        self.assertEqual(('a__b', 'e1'), (first[0], first[1]))
        self.assertEqual([1, 2, 3], [e['seq'] for e in first[2]])
        self.assertFalse(first[3])
        last = calls[-1]
        self.assertTrue(last[3])            # done marker
        self.assertEqual([], last[2])       # nothing new at the end

    def test_follow_splits_large_tails_into_approved_batches(self):
        calls = []
        self.agent.report_progress = lambda project, execution, events, done=False:             calls.append([e['seq'] for e in events]) or 0
        resolution = {'bound': 'exact', 'path': 'T.jsonl'}
        big = [{'kind': 'x'} for _ in range(250)]
        tails = iter([{'events': big, 'offset': 9000},
                      {'events': [], 'offset': 9000}])
        self.agent.follow_progress('a__b', 'e1', str(self.root / 'repo'),
                                   pid=4242, started_at=1000.0,
                                   runner=ScriptedRunner(resolution, tails),
                                   pid_alive=lambda p: False,
                                   clock=lambda: 1000.0,
                                   poll=0.0, idle_limit=1.0)
        # 250 events ride in three posts of at most the approved batch cap
        sizes = [len(c) for c in calls if c]
        self.assertEqual([100, 100, 50], sizes)
        self.assertEqual(list(range(1, 251)),
                         [seq for c in calls for seq in c])

    def test_follow_gives_up_when_the_session_never_binds(self):
        calls = []
        self.agent.report_progress = lambda *a, **k: calls.append(a)
        self.agent.follow_progress('a__b', 'e1', str(self.root / 'repo'),
                                   pid=None, started_at=1000.0,
                                   runner=ScriptedRunner({'bound': None,
                                                          'path': None}),
                                   clock=lambda: 1000.0 + 7200,
                                   poll=0.0, idle_limit=1.0)
        self.assertEqual([], calls)

    def test_follow_survives_report_failures_and_reaches_done(self):
        def boom(*a, **k):
            raise ConnectionError('down')
        self.agent.report_progress = boom
        resolution = {'bound': 'exact', 'path': 'T.jsonl'}
        tails = iter([{'events': [{'kind': 'x'}], 'offset': 100},
                      {'events': [], 'offset': 100}])
        self.agent.follow_progress('a__b', 'e1', str(self.root / 'repo'),
                                   pid=1, started_at=1000.0,
                                   runner=ScriptedRunner(resolution, tails),
                                   pid_alive=lambda p: False,
                                   clock=lambda: 1000.0,
                                   poll=0.0, idle_limit=1.0)
        # reaching the end without raising is the whole assertion

    def test_follow_dispatches_through_the_requested_agents_plugin(self):
        """No runner injected: the requested agent name picks the plugin —
        the default (registered-command) path reads claude, an explicit
        opencode round reads opencode's own output."""
        seen = []
        runner = ScriptedRunner({'bound': None, 'path': None})

        def fake_reader(agent=None):
            seen.append(agent)
            return runner
        self._saved_reader = agent_mod.agent_runners.session_reader
        agent_mod.agent_runners.session_reader = fake_reader
        try:
            self.agent.follow_progress('a__b', 'e1', str(self.root / 'repo'),
                                       pid=None, started_at=1000.0,
                                       agent='opencode',
                                       clock=lambda: 1000.0 + 7200,
                                       poll=0.0, idle_limit=1.0)
        finally:
            agent_mod.agent_runners.session_reader = self._saved_reader
        self.assertEqual(['opencode'], seen)




FAKE_CLAUDE_SOURCE = """\
import json, os, re, sys, time, uuid
prompt = sys.stdin.read()
exec_id = re.search(r'execution=([0-9a-f]{32})', prompt).group(1)
d = os.path.join(os.environ['FAKE_PROJECTS'],
 re.sub(r'[^A-Za-z0-9]', '-', os.environ['FAKE_CWD']))
os.makedirs(d, exist_ok=True)
path = os.path.join(d, uuid.uuid4().hex + '.jsonl')
def line(o): return json.dumps(o) + chr(10)
with open(path, 'w', encoding='utf-8') as f:
    f.write(line({'type': 'queue-operation', 'operation': 'enqueue',
 'timestamp': '2026-09-18T00:00:00.000Z', 'sessionId': 's1',
 'content': 'review round execution=' + exec_id}))
for i in range(3):
    time.sleep(0.4)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line({'type': 'assistant',
 'timestamp': '2026-09-18T00:00:01.000Z',
 'message': {'role': 'assistant', 'content': [
 {'type': 'text', 'text': 'step %d' % i}]}}))
"""



class ProgressEndToEndTests(unittest.TestCase):
    """The real remote path with progress reporting on: WS catch-up hands the
    ledger command to this agent, the round claims/prepares/binds/spawns (the
    session is the only fake — a real child process writing a real transcript
    shape), the follower tails it and the stream lands in the project ledger.
    This is the local==remote contract of spec 2026-09-18, exercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.cfg_dir = self.root / 'projects' / 'a__b'
        self.repo = self.cfg_dir / 'repo'
        self.repo.mkdir(parents=True)
        subprocess.check_output(['git', '-C', str(self.repo), 'init', '-b', 'main'],
                                stderr=subprocess.DEVNULL)
        subprocess.check_output(['git', '-C', str(self.repo), 'config', 'user.email',
                                 't@e.invalid'], stderr=subprocess.DEVNULL)
        subprocess.check_output(['git', '-C', str(self.repo), 'config', 'user.name',
                                 'T'], stderr=subprocess.DEVNULL)
        (self.repo / 'README.md').write_text('x', encoding='utf-8')
        subprocess.check_output(['git', '-C', str(self.repo), 'add', '.'],
                                stderr=subprocess.DEVNULL)
        subprocess.check_output(['git', '-C', str(self.repo), 'commit', '-m', 'init'],
                                stderr=subprocess.DEVNULL)
        subprocess.check_output(['git', '-C', str(self.repo), 'branch',
                                 'feature/mocx/pr88'], stderr=subprocess.DEVNULL)
        self.head_sha = subprocess.check_output(
            ['git', '-C', str(self.repo), 'rev-parse', 'feature/mocx/pr88'],
            stderr=subprocess.DEVNULL, text=True).strip()
        (self.cfg_dir / 'entry.md').write_text('entry', encoding='utf-8')
        (self.cfg_dir / 'rules.md').write_text('rules', encoding='utf-8')
        (self.cfg_dir / 'control-plane.json').write_text(json.dumps({
            'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
            'worktree_root': str(self.root / 'worktrees'),
            'state_dir': str(self.cfg_dir / 'state'),
            'project_key': 'a__b', 'snapshot_ttl': 3600,
            'base': 'main', 'branch_prefixes': ['feature/']}), encoding='utf-8')
        self.cli = str(self.cfg_dir / 'claude.exe')
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')
        self.httpd = make_server(self.coordinator, '127.0.0.1', 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        registry = self.coordinator.registry()
        code = registry.create_enroll_code('agent-box', ['a__b'])
        self.token = registry.enroll(code, '1', '127.0.0.1')['token']
        registry.close()
        self.claude_root = self.root / 'claude-projects'
        # the fake session: a real child process that reads the prompt from
        # stdin (exactly how spawn_worker feeds it), writes a transcript
        # carrying the execution id, streams a few lines, then exits
        self.fake_py = self.root / 'fake_claude.py'
        self.fake_py.write_text(FAKE_CLAUDE_SOURCE, encoding='utf-8')
        # the follower must look for transcripts in the sandbox, not the real
        # ~/.claude/projects this process would otherwise point at
        self._old_projects_root = claude_transcript.PROJECTS_ROOT
        claude_transcript.PROJECTS_ROOT = self.claude_root
        # the agent reads its registrations from this machine's dashboard store
        from codeloop.dash import store as dash_store
        self.agent_db = self.root / 'dash.sqlite'
        db = dash_store.connect(str(self.agent_db))
        try:
            with db:
                db.execute("INSERT INTO projects (org_repo, repo_cwd) VALUES ('a__b', ?)",
                           (str(self.repo),))
        finally:
            db.close()
        self.child_procs = []
        state = self.root / 'agent-state'
        state.mkdir()
        self.worker = WorkerService({
            'server': {'url': 'http://127.0.0.1:%d' % self.port, 'token_env': 'X',
                       'project': 'a__b'},
            'state_dir': str(state), 'repo_root': str(self.repo),
            'worktree_root': str(self.root / 'worktrees')}, client=Client(
                {'url': 'http://127.0.0.1:%d' % self.port, 'token_env': 'X',
                 'project': 'a__b'}, token=self.token))
        self.agent = agent_mod.WorkerAgent(
            server_url='http://127.0.0.1:%d' % self.port, token=self.token,
            popen_fn=self.popen, worker_factory=lambda cfg: self.worker,
            registration_loader=lambda project: registration(self.cfg_dir, self.cli),
            cfg_factory=lambda project, row: self.worker.cfg,
            db_path=str(self.agent_db))
        self.stop = threading.Event()
        threading.Thread(target=self.agent.run, args=(self.stop,),
                         daemon=True).start()

    def tearDown(self):
        self.stop.set()
        claude_transcript.PROJECTS_ROOT = self._old_projects_root
        for proc in self.child_procs:   # Windows: the prompt-file handle must go
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        self.httpd.shutdown()
        self.httpd.server_close()
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def popen(self, argv, cwd=None, stdin=None, stdout=None, stderr=None, **kw):
        env = dict(os.environ, FAKE_PROJECTS=str(self.claude_root),
                   FAKE_CWD=str(self.repo))
        proc = subprocess.Popen([sys.executable, str(self.fake_py)],
                                stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        self.child_procs.append(proc)
        return proc

    def wait_for(self, predicate, timeout=30.0, step=0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(step)
        return None

    def test_round_streams_its_transcript_into_the_ledger(self):
        service = self.coordinator.service_for('a__b')
        try:
            service.store.put_item('review:88', 'reviewer', 'v1', {'pr': {
                'number': 88, 'title': 'PR 88', 'html_url': 'http://g/pr/88',
                'head': {'ref': 'feature/mocx/pr88', 'sha': self.head_sha},
                'base': {'ref': 'main'}}}, ['pr-88'])
            service.set_meta('synced_at', time.time())
            service.set_meta('current_versions', {'review:88': 'v1'})
            service.enqueue_command('review_round', 88)
            # the WS catch-up delivers the pending command; the agent runs the
            # round and acks SPAWNED with the execution it started
            row = self.wait_for(lambda: (service.latest_command('review_round', 88)
                                         or {}).get('state') == 'SPAWNED'
                                and service.latest_command('review_round', 88))
            self.assertIsNotNone(row, 'command never reached SPAWNED')
            execution = row['execution']
            self.assertTrue(execution)
            # hello landed: this machine counts as a registered candidate
            self.assertEqual(['agent-box'], self.coordinator.dispatcher
                             .registrations(['a__b'])['a__b']['registered'])
            # the follower's batches land in the project store, seq ascending,
            # and the terminal marker follows once the fake session exits
            def stream_ready():
                rows = service.store.progress_rows(execution)
                done = service.store.db.execute(
                    "SELECT 1 FROM events WHERE kind='command.progress_done'"
                    " AND payload LIKE ?", ('%' + execution + '%',)).fetchone()
                return (rows, done) if (rows and done) else None
            landed = self.wait_for(stream_ready, timeout=40.0)
            self.assertIsNotNone(landed, 'progress stream never completed')
            rows, _ = landed
            self.assertEqual(list(range(1, len(rows) + 1)),
                             [r['seq'] for r in rows])
            texts = [r['payload'].get('text') for r in rows]
            self.assertTrue(str(texts[0]).startswith('step'),
                            'first streamed event should be the session text')
            self.assertEqual(['step 0', 'step 1', 'step 2'],
                             [t for t in texts if str(t).startswith('step')])
        finally:
            service.store.db.close()


