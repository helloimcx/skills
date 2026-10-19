"""opencode storage reader tests: execution→session binding and cursor-based
replay, all against a fixture storage tree in a temp root.

The binding must be content-exact when the spawned prompt embeds the
execution id (opencode stores the user prompt verbatim) and degrade to a
single-candidate directory-matched time-window, never an ambiguous one.
The parser never raises on malformed files — a foreign CLI's storage
contract is never trusted."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeloop.agent_runners import opencode_storage

EXEC = 'b0e7b8a1eaae4ccbaff45c3d61cf7e6f'
NOW = time.time()
T_MS = int((NOW - 30) * 1000)


def session_json(session_id, directory, created_ms=T_MS):
    return {'id': session_id, 'slug': 's', 'version': '1.1',
            'projectID': 'global', 'directory': directory,
            'title': 't', 'time': {'created': created_ms,
                                   'updated': created_ms + 1000}}


def message_json(message_id, session_id, role, created_ms):
    return {'id': message_id, 'sessionID': session_id, 'role': role,
            'time': {'created': created_ms}}


def part_json(part_id, message_id, session_id, **fields):
    data = {'id': part_id, 'sessionID': session_id, 'messageID': message_id}
    data.update(fields)
    return data


class OpencodeStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_root = opencode_storage.STORAGE_ROOT
        opencode_storage.STORAGE_ROOT = self.root
        self.storage = self.root
        self.repo = 'D:\\wt\\repo'

    def tearDown(self):
        opencode_storage.STORAGE_ROOT = self._old_root
        self.tmp.cleanup()

    # -- fixture builders -----------------------------------------------------
    def write(self, rel, data, mtime=None):
        p = self.storage / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            p.write_text(data, encoding='utf-8')
        else:
            p.write_text(json.dumps(data, ensure_ascii=False),
                         encoding='utf-8')
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def seed_round(self, session_id, directory, with_exec=True,
                   created_ms=T_MS, mtime=None):
        """One opencode round: a session, its user prompt (optionally naming
        the execution) and an assistant message with one part of each kind."""
        self.write('session/p1/%s.json' % session_id,
                   session_json(session_id, directory, created_ms),
                   mtime=mtime if mtime is not None else created_ms / 1000)
        self.write('message/%s/msg_u.json' % session_id,
                   message_json('msg_u', session_id, 'user', created_ms))
        self.write('part/msg_u/prt_1.json',
                   part_json('prt_1', 'msg_u', session_id, type='text',
                             text='评审 PR #1。execution=%s、token=1。' % EXEC
                             if with_exec else '按 PR 扫描流程执行。'))
        self.write('message/%s/msg_a.json' % session_id,
                   message_json('msg_a', session_id, 'assistant',
                                created_ms + 1))
        self.write('part/msg_a/prt_1.json',
                   part_json('prt_1', 'msg_a', session_id, type='text',
                             text='判词：通过。'))
        self.write('part/msg_a/prt_2.json',
                   part_json('prt_2', 'msg_a', session_id, type='reasoning',
                             text='先看 diff 范围。',
                             time={'start': created_ms + 2,
                                   'end': created_ms + 3}))
        self.write('part/msg_a/prt_3.json',
                   part_json('prt_3', 'msg_a', session_id, type='tool',
                             tool='bash',
                             state={'status': 'completed',
                                    'input': {'command': 'gitea diff 1'},
                                    'output': '+ line', 'title': 'diff',
                                    'time': {'start': created_ms + 4,
                                             'end': created_ms + 5}}))
        self.write('part/msg_a/prt_4.json',
                   part_json('prt_4', 'msg_a', session_id,
                             type='step-start'))

    # ---- binding ----
    def test_bind_exact_by_execution_id_in_the_stored_prompt(self):
        self.seed_round('ses_match', self.repo)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertEqual({'bound': 'exact', 'path': 'ses_match'}, hit)

    def test_bind_exact_is_case_insensitive(self):
        self.seed_round('ses_match', self.repo)
        hit = opencode_storage.resolve_session(self.repo, EXEC.upper(),
                                               NOW - 60)
        self.assertEqual('exact', hit['bound'])

    def test_bind_exact_ignores_sessions_without_the_id(self):
        self.seed_round('ses_other', self.repo, with_exec=False)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        # without the id the session is only ever window-bound, never exact
        self.assertEqual({'bound': 'window', 'path': 'ses_other'}, hit)
        # the matching session wins over a non-matching one
        self.seed_round('ses_match', self.repo)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertEqual({'bound': 'exact', 'path': 'ses_match'}, hit)

    def test_bind_window_single_directory_matched_candidate_only(self):
        self.seed_round('solo', self.repo, with_exec=False)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertEqual({'bound': 'window', 'path': 'solo'}, hit)
        # two candidates in the window, same directory: refuse to guess
        self.seed_round('solo2', self.repo, with_exec=False)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertEqual({'bound': None, 'path': None}, hit)

    def test_bind_window_requires_the_registered_directory(self):
        self.seed_round('elsewhere', 'D:\\other\\repo', with_exec=False)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertEqual({'bound': None, 'path': None}, hit)

    def test_bind_none_for_invalid_id_or_stale_sessions(self):
        self.assertEqual({'bound': None, 'path': None},
                         opencode_storage.resolve_session(self.repo, 'nope',
                                                          NOW - 60))
        self.seed_round('old', self.repo, with_exec=False,
                        mtime=NOW - 100000)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertIsNone(hit['bound'])

    def test_binding_survives_a_malformed_session_file(self):
        self.write('session/p1/broken.json', '{not json', mtime=NOW - 5)
        self.seed_round('solo', self.repo, with_exec=False)
        hit = opencode_storage.resolve_session(self.repo, EXEC, NOW - 60)
        self.assertEqual('window', hit['bound'])

    # ---- replay ----
    def seeded_events(self):
        return ['评审 PR #1。execution=%s、token=1。' % EXEC,   # user prompt
                '判词：通过。',                                  # assistant text
                '先看 diff 范围。',                              # reasoning
                '$ gitea diff 1']                                # tool line

    def test_tail_replays_parts_in_write_order(self):
        self.seed_round('ses_match', self.repo)
        r = opencode_storage.tail('ses_match')
        self.assertEqual(self.seeded_events(), [e['text'] for e in r['events']])
        kinds = [('mono' if e['mono'] else ('fold' if e['fold'] else 'text'))
                 for e in r['events']]
        self.assertEqual(['text', 'text', 'fold', 'mono'], kinds)
        # the prompt itself is content, the step-start marker is skipped
        self.assertEqual(1, r['skipped'])
        # timestamps arrive in epoch seconds from the millisecond times
        self.assertAlmostEqual(T_MS / 1000.0, r['events'][0]['t'], places=3)
        # size is the session's total event count — the same units as the
        # cursor, so the client's exhaustion test (offset >= size) holds;
        # bytes carries the part-file byte volume for the KB readout
        self.assertEqual(len(r['events']), r['size'])
        total = sum(p.stat().st_size for p in
                    (self.storage / 'part').glob('*/*.json'))
        self.assertEqual(total, r['bytes'])
        self.assertEqual(len(r['events']), r['offset'])   # cursor consumed all

    def test_tail_tool_event_carries_output_and_error_state(self):
        self.seed_round('ses_match', self.repo)
        self.write('part/msg_a/prt_5.json',
                   part_json('prt_5', 'msg_a', 'ses_match', type='tool',
                             tool='bash',
                             state={'status': 'error',
                                    'input': {'command': 'python -m pytest'},
                                    'metadata': {'output': '1 failed',
                                                 'exit': 1},
                                    'time': {'start': T_MS + 6,
                                             'end': T_MS + 7}}))
        r = opencode_storage.tail('ses_match')
        tool = [e for e in r['events'] if e['text'] == '$ python -m pytest']
        self.assertEqual(1, len(tool))
        self.assertEqual('1 failed', tool[0]['out'])
        self.assertEqual('err', tool[0]['status'])

    def test_tail_resumes_by_cursor_and_picks_up_growth(self):
        self.seed_round('ses_match', self.repo)
        first = opencode_storage.tail('ses_match')
        self.assertEqual([], opencode_storage.tail('ses_match',
                                                   offset=first['offset'])
                         ['events'])
        # a part that lands after the cursor: only the new event comes back
        self.write('part/msg_a/prt_9.json',
                   part_json('prt_9', 'msg_a', 'ses_match', type='text',
                             text='补充说明。'))
        grown = opencode_storage.tail('ses_match', offset=first['offset'])
        self.assertEqual(['补充说明。'], [e['text'] for e in grown['events']])
        self.assertEqual(first['offset'] + 1, grown['offset'])

    def test_tail_limit_windows_the_replay(self):
        self.seed_round('ses_match', self.repo)
        r = opencode_storage.tail('ses_match', offset=1, limit=2)
        self.assertEqual(self.seeded_events()[1:3],
                         [e['text'] for e in r['events']])
        self.assertEqual(3, r['offset'])
        # size is the session total's event count, not the window's
        self.assertEqual(opencode_storage.tail('ses_match')['size'], r['size'])

    def test_tail_survives_a_malformed_part_file(self):
        self.seed_round('ses_match', self.repo)
        self.write('part/msg_a/prt_bad.json', 'not json')
        r = opencode_storage.tail('ses_match')
        self.assertEqual(self.seeded_events(),
                         [e['text'] for e in r['events']])
        self.assertEqual(2, r['skipped'])   # step-start + unreadable file

    def test_tail_of_an_unknown_session_is_empty_never_a_raise(self):
        r = opencode_storage.tail('ses_missing')
        self.assertEqual({'events': [], 'offset': 0, 'size': 0, 'bytes': 0,
                          'skipped': 0}, r)


if __name__ == '__main__':
    unittest.main()
