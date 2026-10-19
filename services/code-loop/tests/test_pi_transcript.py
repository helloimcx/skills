#!/usr/bin/env python3
"""pi session reader tests: execution→session binding and cursor-based
replay against a fixture sessions tree in a temp root.

pi persists a session as JSONL under ``~/.pi/agent/sessions/<munged-cwd>/``;
the first line is a ``type:"session"`` header carrying ``cwd``, and the
round's own prompt is the first ``role:"user"`` message — which is what
makes the exact tier content-exact while never trusting text the session
read later (the 2026-09-18 cross-binding lesson). Parsing is fail-soft."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeloop.agent_runners import pi_transcript

EXEC = 'b0e7b8a1eaae4ccbaff45c3d61cf7e6f'
NOW = time.time()
REPO = 'D:\\wt\\repo'


def header(cwd=REPO, sid='0abc1234-0000-0000-0000-000000000001'):
    return {'type': 'session', 'version': 3, 'id': sid,
            'timestamp': '2026-09-20T10:00:00.000Z', 'cwd': cwd}


def user_entry(text, ts='2026-09-20T10:00:01.000Z'):
    return {'type': 'message', 'id': 'm1', 'parentId': None, 'timestamp': ts,
            'message': {'role': 'user',
                        'content': [{'type': 'text', 'text': text}],
                        'timestamp': 1789000001000}}


def assistant_entry(blocks, ts='2026-09-20T10:00:02.000Z'):
    return {'type': 'message', 'id': 'm2', 'parentId': 'm1', 'timestamp': ts,
            'message': {'role': 'assistant', 'content': blocks,
                        'provider': 'selfhost',
                        'model': 'deepseek-v4-flash-0731-self',
                        'stopReason': 'stop', 'timestamp': 1789000002000}}


def tool_result_entry(tool_call_id, text, is_error=False,
                      ts='2026-09-20T10:00:03.000Z'):
    return {'type': 'message', 'id': 'm3', 'parentId': 'm2', 'timestamp': ts,
            'message': {'role': 'toolResult', 'toolCallId': tool_call_id,
                        'toolName': 'bash',
                        'content': [{'type': 'text', 'text': text}],
                        'isError': is_error, 'timestamp': 1789000003000}}


def tool_call_block(name='bash', call_id='call_1', **args):
    return {'type': 'toolCall', 'id': call_id, 'name': name,
            'arguments': args or {'command': 'git status'}}


class PiTranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_root = pi_transcript.SESSIONS_ROOT
        pi_transcript.SESSIONS_ROOT = self.root

    def tearDown(self):
        pi_transcript.SESSIONS_ROOT = self._old_root
        self.tmp.cleanup()

    # -- fixture builders -----------------------------------------------------
    def write_session(self, name, entries, mtime=None, subdir='D--wt-repo'):
        path = self.root / subdir / (name + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        body = ''.join(json.dumps(e) + '\n' for e in entries)
        path.write_text(body, encoding='utf-8')
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def prompt_session(self, name='s1', subdir='D--wt-repo', mtime=None,
                       cwd=REPO, prompt='review PR 1 execution=' + EXEC):
        return self.write_session(
            name, [header(cwd=cwd), user_entry(prompt)], mtime=mtime,
            subdir=subdir)

    # -- exact tier -----------------------------------------------------------
    def test_exact_binds_by_prompt_execution_id(self):
        path = self.prompt_session()
        binding = pi_transcript.resolve_session(REPO, EXEC, NOW)
        self.assertEqual({'bound': 'exact', 'path': str(path)}, binding)

    def test_exec_id_in_later_text_never_binds_exact(self):
        """The id must come from the round's own prompt: a session that
        merely read/echoed the id later falls to the window tier at best."""
        entries = [header(), user_entry('please review'),
                   assistant_entry([{'type': 'text',
                                     'text': 'looking at execution=' + EXEC}])]
        path = self.write_session('s1', entries)
        binding = pi_transcript.resolve_session(REPO, EXEC, NOW)
        self.assertEqual('window', binding['bound'])
        self.assertEqual(str(path), binding['path'])

    def test_first_user_message_decides(self):
        entries = [header(cwd='E:\\elsewhere'), user_entry('start'),
                   user_entry('follow-up about execution=' + EXEC)]
        self.write_session('s1', entries)
        binding = pi_transcript.resolve_session(REPO, EXEC, NOW)
        self.assertIsNone(binding['bound'])

    def test_exact_scan_is_bounded(self):
        """The binding scan reads at most the newest N sessions: the prompt
        carrying the id sits in the oldest session, outside the scan."""
        for i in range(pi_transcript.SCAN_SESSIONS_MAX + 2):
            self.prompt_session(name='s%02d' % i, mtime=NOW - 120 + i,
                                prompt='unscoped round')
        self.prompt_session(name='s00', mtime=NOW - 120)   # oldest: the round
        binding = pi_transcript.resolve_session(REPO, EXEC, NOW)
        self.assertIsNone(binding['bound'])

    # -- window tier ----------------------------------------------------------
    def test_window_binds_a_single_cwd_match(self):
        path = self.prompt_session(prompt='unscoped round')
        binding = pi_transcript.resolve_session(REPO, EXEC, NOW)
        self.assertEqual({'bound': 'window', 'path': str(path)}, binding)

    def test_window_requires_cwd_match(self):
        self.prompt_session(cwd='E:\\elsewhere', prompt='unscoped round')
        self.assertEqual({'bound': None, 'path': None},
                         pi_transcript.resolve_session(REPO, EXEC, NOW))

    def test_window_needs_a_singleton(self):
        self.prompt_session(name='s1', prompt='unscoped round')
        self.prompt_session(name='s2', prompt='unscoped round')
        self.assertEqual({'bound': None, 'path': None},
                         pi_transcript.resolve_session(REPO, EXEC, NOW))

    def test_stale_sessions_are_ignored(self):
        self.prompt_session(mtime=NOW - 3600)
        self.assertEqual({'bound': None, 'path': None},
                         pi_transcript.resolve_session(REPO, EXEC, NOW))

    def test_session_after_the_window_is_ignored(self):
        """The window tier's upper bound: a session created well after the
        execution started is not a candidate even when the cwd matches."""
        self.prompt_session(prompt='unscoped round', mtime=NOW + 700)
        self.assertEqual({'bound': None, 'path': None},
                         pi_transcript.resolve_session(REPO, EXEC, NOW))

    def test_invalid_exec_id_is_unbound(self):
        self.assertEqual({'bound': None, 'path': None},
                         pi_transcript.resolve_session(REPO, 'nope', NOW))

    # -- tail: replay ---------------------------------------------------------
    def test_replay_maps_blocks_to_flat_events(self):
        path = self.prompt_session()
        path.write_text(''.join(json.dumps(e) + '\n' for e in [
            header(),
            user_entry('review PR 1'),
            assistant_entry([{'type': 'thinking', 'thinking': 'plan the fix'},
                             tool_call_block(),
                             {'type': 'text', 'text': 'done'}]),
            tool_result_entry('call_1', 'On branch main\n', is_error=False),
        ]), encoding='utf-8')
        tail = pi_transcript.tail(str(path))
        kinds = [(e['text'], e['mono'], e['fold'], e['out'], e['status'])
                 for e in tail['events']]
        self.assertEqual([
            ('review PR 1', False, False, None, None),          # the prompt
            ('plan the fix', False, True, None, None),          # thinking folds
            ('$ git status', True, False, 'On branch main\n', None),
            ('done', False, False, None, None),
        ], kinds)
        self.assertEqual(4, len(tail['events']))
        self.assertEqual(tail['offset'], tail['size'])   # drained: bytes cursor

    def test_tool_result_error_sets_status(self):
        path = self.write_session('s1', [
            header(), user_entry('go'),
            assistant_entry([tool_call_block()]),
            tool_result_entry('call_1', 'fatal: not a repo', is_error=True),
        ])
        events = pi_transcript.tail(str(path))['events']
        self.assertEqual('err', events[1]['status'])
        self.assertEqual('fatal: not a repo', events[1]['out'])

    def test_plain_string_user_content(self):
        path = self.write_session('s1', [header()])
        path.write_text(json.dumps(header()) + '\n' + json.dumps(
            {'type': 'message', 'id': 'm1', 'timestamp': '2026-09-20T10:00:01.000Z',
             'message': {'role': 'user', 'content': 'bare',
                         'timestamp': 1789000001000}}) + '\n', encoding='utf-8')
        events = pi_transcript.tail(str(path))['events']
        self.assertEqual(['bare'], [e['text'] for e in events])

    def test_non_message_entries_are_skipped(self):
        path = self.prompt_session()
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'type': 'model_change', 'id': 'mc1',
                                 'provider': 'selfhost',
                                 'modelId': 'deepseek'}) + '\n')
            fh.write('not json at all\n')
        tail = pi_transcript.tail(str(path))
        self.assertEqual(['review PR 1 execution=' + EXEC],
                         [e['text'] for e in tail['events']])
        self.assertEqual(3, tail['skipped'])   # header + model_change + garbage

    # -- tail: cursor mechanics ----------------------------------------------
    def test_incremental_tail_resumes_from_offset(self):
        path = self.prompt_session()
        first = pi_transcript.tail(str(path))
        self.assertEqual(['review PR 1 execution=' + EXEC],
                         [e['text'] for e in first['events']])
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(assistant_entry(
                [{'type': 'text', 'text': 'second'}])) + '\n')
        second = pi_transcript.tail(str(path), offset=first['offset'])
        self.assertEqual(['second'], [e['text'] for e in second['events']])
        self.assertEqual(second['offset'], second['size'])
        self.assertGreater(second['size'], first['size'])

    def test_incomplete_last_line_is_held_back(self):
        path = self.prompt_session()
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(assistant_entry(
                [{'type': 'text', 'text': 'partial'}]))[:20])
        tail = pi_transcript.tail(str(path))
        self.assertEqual(1, len(tail['events']))
        self.assertLess(tail['offset'], path.stat().st_size)

    def test_missing_file_degrades_to_empty(self):
        tail = pi_transcript.tail(str(self.root / 'nope' / 'x.jsonl'))
        self.assertEqual({'events': [], 'offset': 0, 'size': 0, 'skipped': 0},
                         {k: tail[k] for k in ('events', 'offset', 'size',
                                               'skipped')})


if __name__ == '__main__':
    unittest.main()
