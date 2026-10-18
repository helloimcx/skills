"""Claude transcript reader tests: parsing, execution→session binding and
offset-based incremental reads, all against fixture files in a temp root.

The parser must never raise on malformed lines; binding is content-exact when
the prompt embeds the execution id (dashboard prepared rounds) and degrades to
a single-candidate time-window guess, never an ambiguous one."""
import json
import calendar
import os
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

from codeloop.agent_runners import claude_transcript as runlog

EXEC = 'b0e7b8a1eaae4ccbaff45c3d61cf7e6f'


def line(obj):
    return json.dumps(obj, ensure_ascii=False) + '\n'


def fixture_transcript(with_exec=True):
    """Real structure sampled 2026-09-16 (session cbeb8c7e…, PR #184 round),
    condensed; field names are the contract, contents are paraphrased."""
    prompt = ('Read rules.md（规则唯一源）与 prompt.md（项目绑定项）…\r\n'
              '本轮为看板手动定向评审：目标只有 PR #184。'
              + ('execution=%s、token=212、worktree=D:\\wt\\review-pr-184。' % EXEC
                 if with_exec else '按 PR 扫描流程执行。'))
    return ''.join([
        line({'type': 'queue-operation', 'operation': 'enqueue',
              'timestamp': '2026-09-16T14:41:35.744Z', 'sessionId': 's1',
              'content': prompt}),
        line({'type': 'attachment', 'attachment': {'type': 'hook_success'}}),
        line({'type': 'user', 'timestamp': '2026-09-16T14:41:36.100Z',
              'message': {'role': 'user', 'content': prompt}}),
        line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:40.000Z',
              'message': {'role': 'assistant', 'content': [
                  {'type': 'thinking', 'thinking': '先看 diff 范围，再核对 HEAD 语义。',
                   'signature': ''}]}}),
        line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:41.000Z',
              'message': {'role': 'assistant', 'content': [
                  {'type': 'text', 'text': '开始评审 #184。'},
                  {'type': 'tool_use', 'id': 'call_1', 'name': 'Bash',
                   'input': {'command': 'gitea diff 184'}}]}}),
        line({'type': 'user', 'timestamp': '2026-09-16T14:41:42.000Z',
              'message': {'role': 'user', 'content': [
                  {'type': 'tool_result', 'tool_use_id': 'call_1',
                   'content': '+ def _write_body(…)\n+     body = gzip.compress(body)'}]}}),
        line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:45.000Z',
              'message': {'role': 'assistant', 'content': [
                  {'type': 'tool_use', 'id': 'call_2', 'name': 'Bash',
                   'input': {'command': 'python -m pytest tests/x.py -q'}}]}}),
        line({'type': 'user', 'timestamp': '2026-09-16T14:41:46.000Z',
              'message': {'role': 'user', 'content': [
                  {'type': 'tool_result', 'tool_use_id': 'call_2',
                   'content': '1 failed in 0.42s', 'is_error': True}]}}),
        line({'type': 'last-prompt', 'lastPrompt': 'x', 'sessionId': 's1'}),
        line({'type': 'atis-latch', 'atis': '', 'sessionId': 's1'}),
        line({'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': 's1',
              'timestamp': '2026-09-16T14:42:00.000Z'}),
        'this is not json\n',
        line({'type': 'assistant', 'timestamp': '2026-09-16T14:42:10.000Z',
              'message': {'role': 'assistant', 'content': [
                  {'type': 'text', 'text': '\n\n'}]}}),   # blank: dropped
    ])


class RunlogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_root = runlog.PROJECTS_ROOT
        runlog.PROJECTS_ROOT = self.root
        self.repodir = self.root / runlog.munged('D:\\ai\\skills')
        self.repodir.mkdir(parents=True)

    def tearDown(self):
        runlog.PROJECTS_ROOT = self._old_root
        self.tmp.cleanup()

    def write(self, name, body, mtime=None):
        p = self.repodir / name
        # newline='': real transcripts use LF; Windows must not translate to CRLF
        p.write_bytes(body.encode('utf-8'))
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    # ---- munged directory name (observed: D:\ai\skills -> D--ai-skills) ----
    def test_munged_dir_name(self):
        self.assertEqual(runlog.munged('D:\\ai\\skills'), 'D--ai-skills')
        self.assertEqual(runlog.munged('D:\\ai\\ai_quality_analysis'),
                         'D--ai-ai-quality-analysis')

    # ---- parsing: full-content flat events, no category labels ----
    def test_tail_parses_events_in_order(self):
        self.write('s1.jsonl', fixture_transcript())
        r = runlog.tail(self.repodir / 's1.jsonl')
        kinds = [('fold' if e.get('fold') else ('mono' if e.get('mono') else 'text'))
                 for e in r['events']]
        # prompt, thinking(fold), text intro, Bash tool(mono + out), Bash tool(err + out)
        self.assertEqual(len(r['events']), 5, r['events'])
        self.assertEqual(kinds, ['text', 'fold', 'text', 'mono', 'mono'])
        prompt, think, intro, diff, pytest_ev = r['events']
        self.assertIn('PR #184', prompt['text'])
        self.assertIn('HEAD 语义', think['text'])
        self.assertEqual(intro['text'], '开始评审 #184。')
        self.assertEqual(diff['text'], '$ gitea diff 184')
        self.assertIn('_write_body', diff['out'])
        self.assertEqual(pytest_ev['out'], '1 failed in 0.42s')
        self.assertEqual(pytest_ev['status'], 'err')
        for e in r['events']:
            self.assertNotIn('kind', e)          # no category labels
        self.assertGreater(r['offset'], 0)
        self.assertEqual(r['size'], (self.repodir / 's1.jsonl').stat().st_size)
        # 13 lines: 5 events + 7 skipped (2 queue-operations, attachment,
        # last-prompt, atis-latch, malformed, blank text)
        self.assertEqual(r['skipped'], 7)
        # timestamps parsed from ISO Z into epoch seconds (UTC, zone-independent);
        # the first EVENT is the user line (14:41:36.100Z), not the queue-op
        self.assertAlmostEqual(r['events'][0]['t'],
                               calendar.timegm(time.strptime(
                                   '2026-09-16T14:41:36', '%Y-%m-%dT%H:%M:%S')) + 0.1,
                               places=3)

    def test_tail_incremental_no_repeat(self):
        p = self.write('s2.jsonl', fixture_transcript())
        first = runlog.tail(p)
        self.assertGreater(len(first['events']), 0)
        self.assertEqual(first['offset'], p.stat().st_size)   # all lines complete
        second = runlog.tail(p, offset=first['offset'])
        self.assertEqual(second['events'], [])
        # append: only the new event comes back
        with p.open('a', encoding='utf-8') as f:
            f.write(line({'type': 'assistant', 'timestamp': '2026-09-16T14:43:00.000Z',
                          'message': {'role': 'assistant', 'content': [
                              {'type': 'text', 'text': '判词已写。'}]}}))
        grown = runlog.tail(p, offset=first['offset'])
        self.assertEqual([e['text'] for e in grown['events']], ['判词已写。'])

    def test_tail_holds_back_partial_last_line(self):
        body = fixture_transcript()
        self.write('s3.jsonl', body + '{"type":"assistant","timesta')
        r = runlog.tail(self.repodir / 's3.jsonl')
        self.assertTrue(all(e['text'] != '{"type":"assistant","timesta' for e in r['events']))
        # after the file completes, the held-back line parses from the same offset
        self.assertEqual(r['offset'], len(body.encode('utf-8')))

    def test_tail_caps_output_and_text(self):
        big = 'x' * 30000
        self.write('s4.jsonl', ''.join([
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:40.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'c', 'name': 'Bash',
                       'input': {'command': 'big'}}]}}),
            line({'type': 'user', 'timestamp': '2026-09-16T14:41:41.000Z',
                  'message': {'role': 'user', 'content': [
                      {'type': 'tool_result', 'tool_use_id': 'c',
                       'content': big}]}}),
        ]))
        r = runlog.tail(self.repodir / 's4.jsonl')
        self.assertLessEqual(len(r['events'][0]['out']), runlog.OUT_CAP)
        self.assertTrue(r['events'][0]['out'].endswith('…'))

    def test_tail_limits_events_per_call(self):
        body = ''.join(
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:%02d.000Z' % (i % 60),
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'text', 'text': 'e%d' % i}]}})
            for i in range(10))
        self.write('s5.jsonl', body)
        r = runlog.tail(self.repodir / 's5.jsonl', limit=4)
        self.assertEqual(len(r['events']), 4)
        self.assertGreater(r['offset'], 0)
        self.assertLess(r['offset'], (self.repodir / 's5.jsonl').stat().st_size)

    def test_tail_never_raises_on_unexpected_json_shapes(self):
        """Valid JSON of an unexpected shape must degrade to a skipped line —
        a malformed transcript kills the whole run view otherwise (fail-soft
        contract; claude CLI versions may change the line schema)."""
        weird = ''.join([
            'null\n',
            '[1, 2, 3]\n',
            '"a plain string"\n',
            line({'type': 'assistant', 'message': 'not a dict'}),
            line({'type': 'user', 'message': {'role': 'user', 'content': 42}}),
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:40.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'text', 'text': '幸存行'}]}}),
        ])
        self.write('s6.jsonl', weird)
        r = runlog.tail(self.repodir / 's6.jsonl')
        self.assertEqual([e['text'] for e in r['events']], ['幸存行'])

    def test_tail_line_producing_more_events_than_limit_is_not_truncated(self):
        """The limit is line-granular and lossless: a line yielding more events
        than remaining slots goes out whole, the next line is held back, and on
        resume its tool_results degrade to output-only continuations (stateless
        pairing across the boundary) — every byte of content still arrives."""
        results = [{'type': 'tool_result', 'tool_use_id': 'c%d' % i,
                    'content': 'out-%d' % i} for i in range(3)]
        body = ''.join([
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:40.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'tool_use', 'id': 'c%d' % i, 'name': 'Bash',
                       'input': {'command': 'cmd-%d' % i}} for i in range(3)]}}),
            line({'type': 'user', 'timestamp': '2026-09-16T14:41:41.000Z',
                  'message': {'role': 'user', 'content': results}}),
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:42.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'text', 'text': 'after'}]}}),
        ])
        self.write('s7.jsonl', body)
        r = runlog.tail(self.repodir / 's7.jsonl', limit=3)
        self.assertEqual([e['text'] for e in r['events']],
                         ['$ cmd-0', '$ cmd-1', '$ cmd-2'])
        resumed = runlog.tail(self.repodir / 's7.jsonl', offset=r['offset'])
        outs = [e['out'] for e in resumed['events'] if e['out']]
        self.assertEqual(outs, ['out-0', 'out-1', 'out-2'])
        self.assertEqual([e['text'] for e in resumed['events']][-1], 'after')
        # and the whole transcript parses with intact pairing: 3 tool_use
        # events (out filled in place) + the final text = 4 events, no dups
        everything = runlog.tail(self.repodir / 's7.jsonl')
        self.assertEqual(len(everything['events']), 4)
        self.assertEqual([e['out'] for e in everything['events'][:3]],
                         ['out-0', 'out-1', 'out-2'])

    def test_tail_survives_line_larger_than_chunk(self):
        """issue #195：单行 > CHUNK_MAX（8MB）时 chunk 内无完整行，旧实现
        consumed=0 → offset 死锁，该行之后的事件永不渲染。修复后 tail() 必须
        越过超大行继续返回后续事件，offset 前进到文件尾。"""
        huge = 'x' * (runlog.CHUNK_MAX + 1024)   # 单行 > 8MB
        body = ''.join([
            line({'type': 'user', 'timestamp': '2026-09-16T14:41:36.100Z',
                  'message': {'role': 'user', 'content': 'start'}}),
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:40.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'text', 'text': huge}]}}),
            line({'type': 'assistant', 'timestamp': '2026-09-16T14:41:41.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'text', 'text': 'after'}]}}),
        ])
        self.write('s8.jsonl', body)
        r = runlog.tail(self.repodir / 's8.jsonl')
        texts = [e['text'] for e in r['events']]
        self.assertIn('start', texts)
        self.assertIn('after', texts, '超大行之后的事件必须渲染')
        self.assertEqual(r['offset'], len(body.encode('utf-8')),
                         'offset 必须越过超大行前进到文件尾')
        # 增量续读同样不卡死
        resumed = runlog.tail(self.repodir / 's8.jsonl', offset=r['offset'])
        self.assertEqual(resumed['events'], [])
        self.assertEqual(resumed['offset'], r['offset'])

    # ---- binding ----
    def test_bind_exact_by_execution_uuid(self):
        self.write('cbeb8c7e.jsonl', fixture_transcript(with_exec=True),
                   mtime=time.time() - 60)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC, time.time() - 3600)
        self.assertEqual(hit['bound'], 'exact')
        self.assertTrue(hit['path'].endswith('cbeb8c7e.jsonl'))

    def test_bind_exact_ignores_transcripts_without_uuid(self):
        self.write('a.jsonl', fixture_transcript(with_exec=False), mtime=time.time() - 30)
        self.write('b.jsonl', fixture_transcript(with_exec=True), mtime=time.time() - 10)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC, time.time() - 3600)
        self.assertEqual(hit['bound'], 'exact')
        self.assertTrue(hit['path'].endswith('b.jsonl'))

    def test_bind_window_single_candidate_only(self):
        solo = time.time() - 30
        self.write('w1.jsonl', fixture_transcript(with_exec=False), mtime=solo)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC, time.time() - 60)
        self.assertEqual(hit['bound'], 'window')
        self.assertTrue(hit['path'].endswith('w1.jsonl'))
        # two candidates in the window: refuse to guess
        self.write('w2.jsonl', fixture_transcript(with_exec=False), mtime=solo - 5)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC, time.time() - 60)
        self.assertIsNone(hit['bound'])
        self.assertIsNone(hit['path'])

    def test_bind_none_when_no_directory_or_candidates(self):
        hit = runlog.resolve_session('D:\\ai\\nope', EXEC, time.time() - 60)
        self.assertIsNone(hit['bound'])
        self.assertIsNone(hit['path'])
        # directory exists but nothing recent
        self.write('old.jsonl', fixture_transcript(), mtime=time.time() - 100000)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC, time.time() - 60)
        self.assertIsNone(hit['bound'])

    def test_bind_exact_ignores_an_id_the_session_only_read_later(self):
        """The id must come from the round's own opening messages. A session
        that later read another round's file (shared prompt copy, 2026-09-18)
        carries that foreign id in its body — binding on any head occurrence
        made two executions report the SAME session."""
        other = 'f' * 32
        body = (fixture_transcript(with_exec=True)
                + line({'type': 'assistant', 'timestamp': '2026-09-16T14:42:00.000Z',
                        'message': {'role': 'assistant', 'content': [
                            {'type': 'tool_result', 'tool_use_id': 'c',
                             'content': 'other round execution=%s' % other}]}}))
        self.write('foreign.jsonl', body, mtime=time.time() - 60)
        hit = runlog.resolve_session('D:\\ai\\skills', other, time.time() - 3600)
        self.assertIsNone(hit['bound'],
                          'a foreign id read mid-session must not bind')

    def test_bind_exact_ignores_a_foreign_id_in_the_opening_lines(self):
        """Defense in depth: even inside the first lines, only the round's own
        id may match — a transcript whose opening carries someone else's id is
        not this execution's session."""
        other = 'e' * 32
        opening = line({'type': 'queue-operation', 'operation': 'enqueue',
                        'timestamp': '2026-09-16T14:41:35.000Z', 'sessionId': 's9',
                        'content': 'review round execution=%s' % other})
        self.write('other.jsonl', opening, mtime=time.time() - 60)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC, time.time() - 3600)
        self.assertIsNone(hit['bound'],
                          "another round's prompt must not bind this execution")

    def test_bind_case_insensitive_uuid(self):
        self.write('up.jsonl', fixture_transcript(with_exec=True), mtime=time.time() - 60)
        hit = runlog.resolve_session('D:\\ai\\skills', EXEC.upper(), time.time() - 3600)
        self.assertEqual(hit['bound'], 'exact')


class OpencodeStreamTest(unittest.TestCase):
    """The other CLI dialect: `opencode run --format json` writes its own
    event vocabulary to stdout, which the spawn log captures. Types are
    disjoint from claude's (assistant/user vs text/tool_use/step_*), so the
    parser dispatches per line and one reader serves both."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, body, name='round.log'):
        p = self.root / name
        p.write_bytes(body.encode('utf-8'))
        return p

    # Real structure sampled 2026-09-18 (a tool-using opencode round);
    # field names are the contract, contents are paraphrased.
    def fixture(self):
        return ''.join([
            line({'type': 'step_start', 'timestamp': 1789723867426,
                  'sessionID': 'ses_x',
                  'part': {'id': 'prt_1', 'messageID': 'msg_1', 'snapshot': 'abc',
                           'type': 'step-start'}}),
            line({'type': 'tool_use', 'timestamp': 1789723870491, 'sessionID': 'ses_x',
                  'part': {'type': 'tool', 'tool': 'bash', 'callID': 'call-1',
                           'state': {'status': 'completed',
                                     'input': {'command': 'echo hi'},
                                     'output': 'hi\n',
                                     'metadata': {'output': 'hi\n', 'exit': 0},
                                     'title': 'echo hi'},
                           'id': 'prt_2', 'messageID': 'msg_1'}}),
            line({'type': 'text', 'timestamp': 1789723909148, 'sessionID': 'ses_x',
                  'part': {'id': 'prt_3', 'messageID': 'msg_2', 'type': 'text',
                           'text': 'done'}}),
            line({'type': 'step_finish', 'timestamp': 1789723874313,
                  'sessionID': 'ses_x',
                  'part': {'reason': 'tool-calls', 'cost': 0,
                           'tokens': {'total': 19359}}}),
            line({'type': 'tool_use', 'timestamp': 1789723909200, 'sessionID': 'ses_x',
                  'part': {'type': 'tool', 'tool': 'bash', 'callID': 'call-2',
                           'state': {'status': 'completed',
                                     'input': {'command': 'pytest -q'},
                                     'output': '1 failed in 0.42s',
                                     'metadata': {'exit': 1}}}}),
            'garbage not json\n',
        ])

    def test_stream_parses_into_the_same_flat_events(self):
        r = runlog.tail(self.write(self.fixture()))
        kinds = [('fold' if e.get('fold') else ('mono' if e.get('mono') else 'text'))
                 for e in r['events']]
        # step_start/step_finish are token accounting, not round progress
        self.assertEqual(['mono', 'text', 'mono'], kinds)
        tool, answer, failing = r['events']
        self.assertEqual(tool['text'], '$ echo hi')
        self.assertEqual(tool['out'], 'hi\n')
        self.assertIsNone(tool['status'])
        self.assertEqual(answer['text'], 'done')
        self.assertEqual(failing['out'], '1 failed in 0.42s')
        self.assertEqual(failing['status'], 'err')     # exit code 1
        # skipped lines: the garbage line plus the two step_* accounting lines
        self.assertEqual(r['skipped'], 3)

    def test_opencode_timestamps_are_epoch_milliseconds(self):
        r = runlog.tail(self.write(self.fixture()))
        self.assertAlmostEqual(r['events'][0]['t'], 1789723870.491, places=3)

    def test_incremental_resume_does_not_repeat(self):
        p = self.write(self.fixture())
        first = runlog.tail(p)
        self.assertEqual(first['offset'], p.stat().st_size)
        again = runlog.tail(p, offset=first['offset'])
        self.assertEqual([], again['events'])

    def test_mixed_dialects_in_one_stream_both_parse(self):
        body = self.fixture() + ''.join([
            line({'type': 'assistant', 'timestamp': '2026-09-18T09:04:45.000Z',
                  'message': {'role': 'assistant', 'content': [
                      {'type': 'text', 'text': 'claude 侧正文'}]}}),
            line({'type': 'user', 'timestamp': '2026-09-18T09:04:46.000Z',
                  'message': {'role': 'user', 'content': 'claude 侧提问'}}),
        ])
        r = runlog.tail(self.write(body))
        self.assertIn('claude 侧正文', [e['text'] for e in r['events']])
        self.assertIn('claude 侧提问', [e['text'] for e in r['events']])

    def test_absurd_timestamps_stay_fail_soft(self):
        """A timestamp that cannot be converted (an absurd integer) is a
        missing timestamp, not a crash: the stream must survive a hostile or
        corrupt line rather than 500 the view and kill the follower."""
        body = ''.join([
            line({'type': 'text', 'timestamp': 10 ** 400,
                  'part': {'type': 'text', 'text': 'huge'}}),
            line({'type': 'text', 'timestamp': '99999-01-01T00:00:00Z',
                  'part': {'type': 'text', 'text': 'far future'}}),
            line({'type': 'text', 'timestamp': 1789723909148,
                  'part': {'type': 'text', 'text': 'sane'}}),
        ])
        r = runlog.tail(self.write(body))
        self.assertEqual(['huge', 'far future', 'sane'],
                         [e['text'] for e in r['events']])
        self.assertIsNone(r['events'][0]['t'])
        self.assertIsNone(r['events'][1]['t'])
        self.assertAlmostEqual(r['events'][2]['t'], 1789723909.148, places=3)

    def test_tool_without_state_is_fail_soft(self):
        body = ''.join([
            line({'type': 'tool_use', 'timestamp': 1789723870491,
                  'part': {'type': 'tool', 'tool': 'bash'}}),
            line({'type': 'tool_use', 'timestamp': 1789723870492,
                  'part': {'type': 'tool', 'tool': 'bash', 'state': None}}),
            line({'type': 'text', 'timestamp': 1789723870493, 'part': {}}),
            line({'type': 'text', 'timestamp': 1789723870494,
                  'part': {'text': '   '}}),
        ])
        r = runlog.tail(self.write(body))
        # A tool line without input/state still renders through the shared
        # helper and carries no result — the shape is degenerate, not fatal.
        self.assertEqual(['bash {}', 'bash {}'], [e['text'] for e in r['events']])
        self.assertIsNone(r['events'][0]['out'])
        self.assertIsNone(r['events'][0]['status'])


if __name__ == '__main__':
    unittest.main()
