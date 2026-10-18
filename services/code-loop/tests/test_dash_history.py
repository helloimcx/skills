"""Execution-history data plane tests (spec
docs/specs/2026-09-21-dash-execution-history.md): agent_runs derivation from
the local CLI session stores (zcode child sessions, claude Task pairs), the
unified /api/history feed, the /api/run agent-run id branch, the command-ledger
mirror the loop rows resolve their agent from, and retention.

Every foreign storage contract (zcode sqlite, claude JSONL) is faked with the
minimal observed schema; the readers must degrade fail-soft on anything else."""
import json
import sqlite3
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

from codeloop.dash import agentruns, historyapi, serve, store

BASE_S = time.time() - 3600.0          # an hour ago (must survive the 90d sweep)
ZCODE_SESS = 'sess_subagent_agent_b19b'
AGENT_ID = 'zcode:' + ZCODE_SESS
TOOL_USE_ID = 'toolu_01abc'


def _tool_part(name, status, output='ok', at_ms=None):
    return json.dumps({'type': 'tool', 'tool': name,
                       'state': {'status': status, 'input': {'command': name},
                                 'output': output}}), at_ms


def _text_part(kind, text, at_ms=None):
    return json.dumps({'type': kind, 'text': text}), at_ms


def _make_zcode_db(root, sessions):
    """sessions: [(session_id, created_ms, updated_ms, [(data_json, at_ms), …])]"""
    path = Path(root) / 'db.sqlite'
    con = sqlite3.connect(str(path))
    con.execute('CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT,'
                ' directory TEXT, title TEXT, task_type TEXT,'
                ' time_created INTEGER, time_updated INTEGER)')
    con.execute('CREATE TABLE part (id INTEGER PRIMARY KEY AUTOINCREMENT,'
                ' session_id TEXT, data TEXT, time_created INTEGER,'
                ' time_updated INTEGER)')
    for sid, created, updated, parts in sessions:
        con.execute('INSERT INTO session VALUES (?,?,?,?,?,?,?)',
                    (sid, 'sess_parent', 'D:\\ai\\demo', '评审一个 bug 修复',
                     'subagent_child', created, updated))
        for data, at_ms in parts:
            con.execute('INSERT INTO part (session_id, data, time_created,'
                        ' time_updated) VALUES (?,?,?,?)',
                        (sid, data, at_ms, at_ms))
    con.commit()
    con.close()
    return path


def _claude_lines(tmp_prompt='评审这个 PR'):
    assistant = json.dumps({
        'type': 'assistant', 'timestamp': '2026-09-21T02:00:00Z', 'cwd': 'D:\\ai\\demo',
        'message': {'content': [{'type': 'tool_use', 'id': TOOL_USE_ID,
                                 'name': 'Task',
                                 'input': {'prompt': tmp_prompt}}]}})
    user = json.dumps({
        'type': 'user', 'timestamp': '2026-09-21T02:09:00Z',
        'message': {'content': [{'type': 'tool_result', 'tool_use_id': TOOL_USE_ID,
                                 'content': 'Approved（1 条建议）'}]}})
    return assistant, user


class AgentHistoryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.zcode_db = self.root / 'zcode' / 'db.sqlite'
        self.claude_dir = self.root / 'claude' / 'projects'
        self.oc_dir = self.root / 'oc' / 'storage'
        self.pi_dir = self.root / 'pi' / 'sessions'
        from codeloop.agent_runners import opencode_storage as _ocs
        from codeloop.agent_runners import pi_transcript as _pit
        self._old = (agentruns.ZCODE_DB, agentruns.CLAUDE_PROJECTS,
                     agentruns.OPENCODE_STORAGE, agentruns.PI_SESSIONS,
                     _ocs.STORAGE_ROOT, _pit.SESSIONS_ROOT)
        agentruns.ZCODE_DB = self.zcode_db
        agentruns.CLAUDE_PROJECTS = self.claude_dir
        agentruns.OPENCODE_STORAGE = self.oc_dir
        agentruns.PI_SESSIONS = self.pi_dir
        _ocs.STORAGE_ROOT = self.oc_dir
        _pit.SESSIONS_ROOT = self.pi_dir
        self.db = store.connect(self.root / 'dash.sqlite')
        self.addCleanup(self._teardown)

    def _teardown(self):
        from codeloop.agent_runners import opencode_storage as _ocs
        from codeloop.agent_runners import pi_transcript as _pit
        (agentruns.ZCODE_DB, agentruns.CLAUDE_PROJECTS,
         agentruns.OPENCODE_STORAGE, agentruns.PI_SESSIONS,
         _ocs.STORAGE_ROOT, _pit.SESSIONS_ROOT) = self._old
        self.db.close()
        self.tmp.cleanup()

    def _seed_loop(self, agent='opencode'):
        """One mirrored loop execution (review:184) with a command ledger row."""
        pid = store.upsert_project(self.db, 'a__b', '')
        store.replace_runtime(self.db, pid, {
            'work_items': [{'id': 1, 'key': 'review:184', 'kind': 'reviewer',
                            'state': 'RUNNING', 'priority': 1, 'version': 3,
                            'payload': json.dumps(
                                {'pr': {'number': 184, 'title': 'gzip 压缩'}}),
                            'resources': None, 'created_at': str(BASE_S)}],
            'executions': [{'id': 'b' * 32, 'item_id': 1, 'worker': 'h/r',
                            'state': 'SUCCEEDED', 'result': None, 'worktree': None,
                            'branch': None, 'head': None, 'expires_at': None,
                            'created_at': str(BASE_S + 60)}],
            'commands': [{'id': 7, 'action': 'review_round', 'number': 184,
                          'state': 'SUCCEEDED', 'execution': 'b' * 32,
                          'agent': agent, 'model': None,
                          'created_at': BASE_S + 30}]})
        return pid


class SchemaTests(AgentHistoryTestBase):
    def test_connect_creates_tables_and_index(self):
        names = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE name in "
            "('agent_runs','commands','idx_agent_runs_time')")}
        self.assertEqual(names, {'agent_runs', 'commands', 'idx_agent_runs_time'})


class ZcodeTests(AgentHistoryTestBase):
    def _write(self, parts, updated_ms=int(BASE_S * 1000) + 540_000,
               created_ms=int(BASE_S * 1000)):
        self.zcode_db.parent.mkdir(parents=True, exist_ok=True)
        return _make_zcode_db(self.zcode_db.parent, [
            (ZCODE_SESS, created_ms, updated_ms, parts)])

    def test_finished_session_derives_archive(self):
        self._write([_tool_part('Bash', 'completed', '9 passed', BASE_S * 1000),
                     _text_part('text', '评审结论：Approved', BASE_S * 1000 + 500_000)])
        runs, cursor = agentruns.ZcodeSource.enumerate(0)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run['id'], AGENT_ID)
        self.assertEqual(run['state'], 'SUCCEEDED')
        self.assertEqual(run['duration_s'], 540)
        self.assertIn('Approved', run['result_head'])
        self.assertEqual(json.loads(run['tool_counts']), {'Bash': 1})
        self.assertGreater(cursor, 0)

    def test_running_part_marks_live_and_refresh_flips(self):
        self._write([_tool_part('Read', 'running', None, BASE_S * 1000)])
        notes = agentruns.sweep(self.db)
        row = self.db.execute('SELECT * FROM agent_runs').fetchone()
        self.assertEqual(row['state'], 'RUNNING')
        self.assertIn('1 run(s)', notes['zcode'])
        # the tool finishes between sweeps: cursor moved past, the RUNNING
        # rescan must still flip the archive to its terminal state
        con = sqlite3.connect(str(self.zcode_db))
        data, = con.execute('SELECT data FROM part').fetchone()
        con.execute('UPDATE part SET data=?',
                    (data.replace('"running"', '"completed"'),))
        con.commit()
        con.close()
        agentruns.sweep(self.db, now=time.time() + 999)
        row = self.db.execute('SELECT state FROM agent_runs').fetchone()
        self.assertEqual(row['state'], 'SUCCEEDED')

    def test_missing_store_degrades_without_cursor_advance(self):
        runs, cursor = agentruns.ZcodeSource.enumerate(123.0)
        self.assertEqual((runs, cursor), ([], 123.0))
        notes = agentruns.sweep(self.db)
        self.assertEqual(notes['zcode'], '0 run(s)')

    def test_cursor_overlap_window_revisits_recent_updates(self):
        # 批次不满时游标回退 60s 重叠窗（max(since, now-60)）：SELECT 快照后
        # 提交的更新下轮被重扫，upsert 幂等保证重叠免费（bc3ab15 游标重叠窗）。
        at = BASE_S * 1000
        self._write([_tool_part('Bash', 'completed', '9 passed', at)])
        runs, cursor = agentruns.ZcodeSource.enumerate(0)
        self.assertEqual(len(runs), 1)
        self.assertLessEqual(cursor, time.time() - agentruns.SWEEP_OVERLAP_SECONDS + 1)
        self.assertGreater(cursor, 0)
        # 快照后会话更新（time_updated 落在重叠窗内）→ 下轮 enumerate 仍能重扫
        con = sqlite3.connect(str(self.zcode_db))
        con.execute('UPDATE session SET time_updated=? WHERE id=?',
                    (int(time.time() * 1000) - 10_000, ZCODE_SESS))
        con.commit()
        con.close()
        runs2, cursor2 = agentruns.ZcodeSource.enumerate(cursor)
        self.assertEqual(len(runs2), 1)
        self.assertGreaterEqual(cursor2, cursor)

    def test_session_events_stream_flat_and_incremental(self):
        at = BASE_S * 1000
        self._write([_tool_part('Bash', 'completed', '9 passed', at),
                     _text_part('reasoning', '先看 diff 范围', at + 1000),
                     _text_part('text', '结论：Approved', at + 2000)])
        run = {'id': AGENT_ID, 'platform': 'zcode',
               'transcript_ref': 'zcode db · session ' + ZCODE_SESS}
        stream = agentruns.session_events(run, 0)
        self.assertEqual(stream['bound'], 'agent-session')
        self.assertEqual(len(stream['events']), 3)
        tool, think, text = stream['events']
        self.assertTrue(tool['mono'])
        self.assertEqual(tool['out'], '9 passed')
        self.assertTrue(think['fold'])
        self.assertIn('Approved', text['text'])
        again = agentruns.session_events(run, stream['offset'])
        self.assertEqual(again['events'], [])


class ClaudeTests(AgentHistoryTestBase):
    def _write(self, extra_lines=()):
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        project = self.claude_dir / 'D--ai-demo'
        project.mkdir(exist_ok=True)
        lines = list(_claude_lines()) + list(extra_lines)
        path = project / 'aaaa1111.jsonl'
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    def test_task_pair_derived_and_idempotent(self):
        self._write()
        runs, _ = agentruns.ClaudeSource.enumerate(0)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run['platform'], 'claude')
        self.assertEqual(run['state'], 'SUCCEEDED')
        self.assertEqual(run['duration_s'], 540)
        self.assertEqual(run['project'], 'D:\\ai\\demo')
        self.assertIn('toolu_01abc', run['id'])
        agentruns.sweep(self.db)
        agentruns.sweep(self.db, now=time.time() + 60)
        count, = self.db.execute('SELECT COUNT(*) FROM agent_runs').fetchone()
        self.assertEqual(count, 1)

    def test_malformed_lines_skipped(self):
        self._write(extra_lines=['{not json', 'garbage', '[]'])
        runs, _ = agentruns.ClaudeSource.enumerate(0)
        self.assertEqual(len(runs), 1)

    def test_detail_degrades_to_timeline(self):
        self._write()
        agentruns.sweep(self.db)
        payload = historyapi.agent_run_detail(self.db, 'claude:aaaa1111#%s'
                                              % TOOL_USE_ID, 0)
        self.assertIsNotNone(payload)
        self.assertIsNone(payload['session']['bound'])
        self.assertEqual(len(payload['timeline']), 3)


class SweepTests(AgentHistoryTestBase):
    def test_retention_ages_out_finished_but_keeps_running(self):
        old = time.time() - 400 * 86400
        with self.db:
            self.db.execute('INSERT INTO agent_runs (id, platform, state, started_at)'
                            ' VALUES (?,?,?,?)', ('zcode:old', 'zcode', 'SUCCEEDED', old))
            self.db.execute('INSERT INTO agent_runs (id, platform, state, started_at)'
                            ' VALUES (?,?,?,?)',
                            ('zcode:live', 'zcode', 'RUNNING', old))
        agentruns.sweep(self.db, now=time.time())
        ids = {r[0] for r in self.db.execute('SELECT id FROM agent_runs')}
        self.assertEqual(ids, {'zcode:live'})


class HistoryTests(AgentHistoryTestBase):
    def _seed(self):
        self._seed_loop(agent='opencode')
        with self.db:
            self.db.execute(
                'INSERT INTO agent_runs (id, platform, project, task_prompt, state,'
                ' started_at, duration_s) VALUES (?,?,?,?,?,?,?)',
                (AGENT_ID, 'zcode', 'D:\\ai\\demo', '评审一个 bug 修复',
                 'SUCCEEDED', BASE_S + 120, 540))

    def test_merged_feed_sorted_and_filtered(self):
        self._seed()
        feed = historyapi.history(self.db, {})
        self.assertEqual(feed['total'], 2)
        self.assertEqual([r['kind'] for r in feed['rows']], ['agent', 'loop'])
        self.assertEqual(set(feed['agents']), {'opencode', 'zcode'})
        loops = historyapi.history(self.db, {'source': 'loop'})
        self.assertEqual(loops['rows'][0]['agent'], 'opencode')
        self.assertEqual(loops['rows'][0]['loop'], 'review')
        self.assertEqual(loops['rows'][0]['title'], 'gzip 压缩')
        only_agent = historyapi.history(self.db, {'agent': 'zcode'})
        self.assertEqual([r['id'] for r in only_agent['rows']], [AGENT_ID])
        # facet keeps offering the other agents while one is selected
        self.assertEqual(set(only_agent['agents']), {'opencode', 'zcode'})
        self.assertEqual(historyapi.history(
            self.db, {'state': 'RUNNING'})['total'], 0)
        self.assertEqual(historyapi.history(
            self.db, {'q': 'gzip'})['rows'][0]['kind'], 'loop')
        self.assertEqual(historyapi.history(
            self.db, {'project': 'a__b'})['total'], 1)

    def test_loop_agent_defaults_to_claude_without_ledger(self):
        self._seed_loop()
        self.db.execute('DELETE FROM commands')
        self.db.commit()
        feed = historyapi.history(self.db, {'source': 'loop'})
        self.assertEqual(feed['rows'][0]['agent'], 'claude')

    def test_agent_summary_extracts_structured_task(self):
        # code-loop 模板 prompt → 结构化摘要（循环 + 轮次 + PR 目标 + 动作）
        self.assertEqual(
            historyapi._agent_summary(
                '你是 code-loop reviewer 循环的独立 Review Subagent（只读角色）。'
                '对 Gitea PR #344 评审变更，输出 APPROVE 或 REQUEST_CHANGES。'),
            'reviewer · 评审 · PR #344')
        # discover Inspection 轮次
        self.assertEqual(
            historyapi._agent_summary(
                '你是 code-loop discover 循环的 Inspection 子代理（第 40 轮）。'
                '工作目录 D:\\ai\\skills。'),
            'discover · 第 40 轮 · 巡检')
        # 无 code-loop 模板但有动作词 → 动作摘要
        self.assertEqual(
            historyapi._agent_summary('评审一个 bug 修复'),
            '评审')
        # 完全无模式 → 兜底返回原文截断
        self.assertEqual(
            historyapi._agent_summary('随便一段没有关键词的文本'),
            '随便一段没有关键词的文本')


class RunDetailBranchTests(AgentHistoryTestBase):
    def test_agent_id_serves_archive_payload(self):
        at = BASE_S * 1000
        self._write = self.zcode_db.parent.mkdir(parents=True, exist_ok=True)
        _make_zcode_db(self.zcode_db.parent, [
            (ZCODE_SESS, at, at + 540_000,
             [_tool_part('Bash', 'completed', '9 passed', at),
              _text_part('text', '结论：Approved', at + 500_000)])])
        agentruns.sweep(self.db)
        payload = serve.run_detail(self.db, None, AGENT_ID, 0)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['execution']['state'], 'SUCCEEDED')
        self.assertEqual(payload['execution']['source'], 'agent')
        self.assertIsNone(payload['item']['number'])
        self.assertEqual(payload['session']['bound'], 'agent-session')
        self.assertGreater(len(payload['session']['events']), 0)
        # incremental: a since cursor at the offset drains to zero new events
        offset = payload['session']['offset']
        again = serve.run_detail(self.db, None, AGENT_ID, offset)
        self.assertEqual(again['session']['events'], [])

    def test_unknown_agent_id_maps_to_none(self):
        self.assertIsNone(serve.run_detail(self.db, None, 'zcode:sess_nope', 0))

    def test_route_preserves_case_sensitive_agent_id(self):
        # review BLOCKING-1: exec 必须原样到达档案查询——claude 档案 id 内嵌
        # 大小写敏感的 tool_use_id，HTTP 层曾整体 lower 导致大写 id 恒 404
        mixed = 'claude:aaaa1111#toolu_01AbC123'
        self.db.execute(
            'INSERT INTO agent_runs (id, platform, project, state, started_at)'
            ' VALUES (?,?,?,?,?)', (mixed, 'claude', 'D:\ai\demo',
                                    'SUCCEEDED', BASE_S))
        self.db.commit()
        ctype, status, payload = serve.route(self.db, '/api/run',
                                             {'exec': mixed, 'org': ''})
        self.assertEqual(status, 200)
        self.assertEqual(payload['execution']['id'], mixed)
        # 反向：整串小写后不再是同一档案（大小写敏感契约本身不被破坏）
        ctype, status, _ = serve.route(self.db, '/api/run',
                                       {'exec': mixed.lower(), 'org': ''})
        self.assertEqual(status, 404)

    def test_projects_facet_covers_unregistered_and_survives_filter(self):
        self._seed_loop(agent='opencode')
        with self.db:
            self.db.execute(
                'INSERT INTO agent_runs (id, platform, project, state, started_at)'
                ' VALUES (?,?,?,?,?)', (AGENT_ID, 'zcode', 'D:\ai\demo',
                                        'SUCCEEDED', BASE_S + 120))
        feed = historyapi.history(self.db, {})
        self.assertEqual(set(feed['projects']), {'a__b', 'D:\ai\demo'})
        scoped = historyapi.history(self.db, {'project': 'D:\ai\demo'})
        self.assertEqual(scoped['total'], 1)
        self.assertEqual(set(scoped['projects']), {'a__b', 'D:\ai\demo'})

    def test_serve_route_rejects_invalid_and_serves_history(self):
        status = serve.route(self.db, '/api/run', {'exec': 'zzz'})
        self.assertEqual(status[1], 400)
        status = serve.route(self.db, '/api/run',
                             {'exec': AGENT_ID, 'org': ''})
        self.assertEqual(status[1], 404)   # archive absent → unknown execution
        self.db.execute(
            'INSERT INTO agent_runs (id, platform, project, state, started_at)'
            ' VALUES (?,?,?,?,?)', (AGENT_ID, 'zcode', 'D:\\ai\\demo',
                                    'SUCCEEDED', BASE_S))
        self.db.commit()
        ctype, status, payload = serve.route(self.db, '/api/run',
                                             {'exec': AGENT_ID, 'org': ''})
        self.assertEqual(status, 200)
        self.assertEqual(payload['execution']['source'], 'agent')
        ctype, status, payload = serve.route(self.db, '/api/history', {})
        self.assertEqual(status, 200)
        self.assertIn('agents', payload)


def _oc_session(root, sid, prompt, with_tool=True):
    """Minimal opencode storage tree: session + first user message + parts."""
    storage = Path(root)
    ses_dir = storage / 'session' / 'proj1'
    ses_dir.mkdir(parents=True, exist_ok=True)
    (ses_dir / ('%s.json' % sid)).write_text(json.dumps({
        'id': sid, 'directory': 'D:\ai\demo',
        'time': {'created': int(BASE_S * 1000), 'updated': int(BASE_S * 1000) + 540_000},
    }), encoding='utf-8')
    msg_dir = storage / 'message' / sid
    msg_dir.mkdir(parents=True, exist_ok=True)
    (msg_dir / 'msg01.json').write_text(json.dumps({
        'id': 'msg01', 'role': 'user',
        'time': {'created': int(BASE_S * 1000)}}), encoding='utf-8')
    (msg_dir / 'msg02.json').write_text(json.dumps({
        'id': 'msg02', 'role': 'assistant',
        'time': {'created': int(BASE_S * 1000) + 500_000}}), encoding='utf-8')
    part_dir = storage / 'part' / 'msg01'
    part_dir.mkdir(parents=True, exist_ok=True)
    (part_dir / 'p1.json').write_text(json.dumps(
        {'id': 'p1', 'type': 'text', 'text': prompt}), encoding='utf-8')
    if with_tool:
        part_dir2 = storage / 'part' / 'msg02'
        part_dir2.mkdir(parents=True, exist_ok=True)
        (part_dir2 / 'p2.json').write_text(json.dumps(
            {'id': 'p2', 'type': 'tool', 'tool': 'Bash',
             'state': {'status': 'completed', 'input': {'command': 'pytest -q'},
                       'output': '9 passed'}}), encoding='utf-8')


class OpencodeTests(AgentHistoryTestBase):
    def _write(self, prompt='本轮指令：定向评审 PR #9（execution=abc，token=1）'):
        _oc_session(self.oc_dir, 'ses-oc-1', prompt)

    def test_spawn_marker_session_derived(self):
        self._write()
        runs, _ = agentruns.OpencodeSource.enumerate(0)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run['platform'], 'opencode')
        self.assertIn('ses-oc-1', run['id'])
        self.assertEqual(run['state'], 'SUCCEEDED')
        self.assertEqual(json.loads(run['tool_counts']), {'Bash': 1})

    def test_interactive_session_skipped(self):
        self._write(prompt='帮我看看这个函数怎么写')
        runs, _ = agentruns.OpencodeSource.enumerate(0)
        self.assertEqual(runs, [])

    def test_stream_replays_through_shared_reader(self):
        self._write()
        run = {'id': 'opencode:ses-oc-1', 'platform': 'opencode',
               'transcript_ref': 'ses-oc-1'}
        stream = agentruns.session_events(run, 0)
        self.assertEqual(stream['bound'], 'agent-session')
        kinds = [e['mono'] for e in stream['events']]
        self.assertIn(True, kinds)          # the tool line is mono
        self.assertTrue(any('pytest' in e['text'] for e in stream['events']))


def _pi_session(root, stem, prompt, tool_calls=(('Bash', 't1'),)):
    """Minimal pi JSONL session: header + first user message + toolCall block."""
    path = Path(root) / '--D--ai-demo--' / ('%s.jsonl' % stem)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({'type': 'session', 'cwd': 'D:\ai\demo'}),
        json.dumps({'type': 'message', 'timestamp': '2026-09-21T02:00:00Z',
                    'message': {'role': 'user',
                                'content': [{'type': 'text', 'text': prompt}]}}),
        json.dumps({'type': 'message', 'timestamp': '2026-09-21T02:01:00Z',
                    'message': {'role': 'assistant', 'content': [
                        {'type': 'toolCall', 'id': tool_calls[0][1],
                         'name': tool_calls[0][0], 'arguments': {}}]}}),
        json.dumps({'type': 'message', 'timestamp': '2026-09-21T02:02:00Z',
                    'message': {'role': 'toolResult', 'toolCallId': tool_calls[0][1],
                                'content': [{'type': 'text', 'text': 'ok'}],
                                'isError': False}}),
    ]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


class PiTests(AgentHistoryTestBase):
    def _write(self, prompt='本轮指令：执行修复（execution=xyz）'):
        return _pi_session(self.pi_dir, '2026-09-21T02-00-00-000Z_pi-sess-1', prompt)

    def test_spawn_marker_session_derived(self):
        path = self._write()
        runs, _ = agentruns.PiSource.enumerate(0)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run['platform'], 'pi')
        self.assertIn('pi-sess-1', run['id'])
        self.assertEqual(run['project'], 'D:\ai\demo')
        self.assertEqual(json.loads(run['tool_counts']), {'Bash': 1})
        self.assertEqual(run['transcript_ref'], str(path))

    def test_interactive_session_skipped(self):
        self._write(prompt='早上好，继续昨天的话题')
        runs, _ = agentruns.PiSource.enumerate(0)
        self.assertEqual(runs, [])

    def test_stream_replays_and_refresh_recovers(self):
        self._write()
        agentruns.sweep(self.db)
        run = dict(self.db.execute(
            "SELECT * FROM agent_runs WHERE platform='pi'").fetchone())
        stream = agentruns.session_events(run, 0)
        self.assertTrue(stream['events'])
        self.assertTrue(any(e['mono'] for e in stream['events']))


class MirrorTests(AgentHistoryTestBase):
    def test_copy_runtime_mirrors_command_ledger(self):
        pid = store.upsert_project(self.db, 'a__b', '')
        cp_db = self.root / 'cp' / 'control-plane.sqlite'
        cp_db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(cp_db))
        con.executescript(
            'CREATE TABLE commands (id INTEGER PRIMARY KEY, action TEXT,'
            ' number INTEGER, state TEXT, execution TEXT, agent TEXT,'
            ' model TEXT, created_at REAL);')
        con.execute('INSERT INTO commands VALUES (?,?,?,?,?,?,?,?)',
                    (7, 'review_round', 184, 'SPAWNED', 'b' * 32, 'pi', None,
                     BASE_S))
        con.commit()
        con.close()
        collect_copy(pid, str(cp_db), self)
        row = self.db.execute(
            'SELECT agent, execution FROM commands WHERE project_id=?',
            (pid,)).fetchone()
        self.assertEqual((row['agent'], row['execution']), ('pi', 'b' * 32))


def collect_copy(pid, cp_db, test):
    from codeloop.dash.collect import copy_runtime
    copy_runtime(test.db, pid, cp_db)


if __name__ == '__main__':
    unittest.main()
