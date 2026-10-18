"""GitHub contract tests use real HTTP, no production network."""
import base64
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

for p in (str(Path(__file__).resolve().parents[1]),
          str(Path(__file__).resolve().parents[1] / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from codeloop.cp import runtime
from codeloop.cp.github import GitHub, GitHubError


def issue(n, **extra):
    return dict(number=n, state='open', title='Issue', body='', **extra)


class GitHubAdapterTests(unittest.TestCase):
    def setUp(self):
        self.routes = {}
        self.calls = []
        self.queries = []
        self.received_headers = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def do_PATCH(self):
                self.respond()

            def respond(self):
                url = urlparse(self.path)
                path = url.path.replace('/repos/a/b', '')
                page = int(parse_qs(url.query).get('page', ['0'])[0])
                data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                outer.calls.append((self.command, path, page, data))
                outer.queries.append(url.query)
                outer.received_headers.append(self.headers)
                response = outer.routes.get((self.command, path, page), [])
                if response == 'disconnect':
                    self.close_connection = True
                    return
                if response == 'missing':
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'Not Found')
                    return
                if isinstance(response, tuple) and response and response[0] == 'raw':
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(response[1].encode('utf-8'))
                    return
                status = 500 if response == 'error' else 200
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = GitHub({'url': 'http://127.0.0.1:%d' % self.server.server_port,
                              'repo': 'a/b', 'page_limit': 1})
        self.routes[('GET', '/branches/main', 0)] = {'commit': {'sha': 'base'}}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_headers_sent(self):
        self.routes[('GET', '/issues', 1)] = []
        self.client.list_issues()
        h = self.received_headers[-1]
        self.assertEqual('code-loop', h.get('User-Agent'))
        self.assertEqual('2022-11-28', h.get('X-GitHub-Api-Version'))
        self.assertIn('application/vnd.github', h.get('Accept', ''))

    def test_complete_snapshot(self):
        self.routes[('GET', '/issues', 1)] = [issue(1)]
        self.routes[('GET', '/issues', 2)] = [issue(2)]
        self.routes[('GET', '/issues/1/comments', 1)] = [{'id': 1, 'body': 'one'}]
        self.routes[('GET', '/issues/1/comments', 2)] = [{'id': 2, 'body': 'two'}]
        pr = issue(3, head={'sha': 'abc', 'ref': 'feature/x'},
                   base={'sha': 'base', 'ref': 'main'}, merged=False)
        self.routes[('GET', '/pulls', 1)] = [pr]
        self.routes[('GET', '/pulls/3', 0)] = pr
        self.routes[('GET', '/pulls/3/reviews', 1)] = [{'id': 10, 'state': 'CHANGES_REQUESTED', 'body': 'fix this'}]
        self.routes[('GET', '/commits/abc/statuses', 1)] = [{'id': 20, 'context': 'ci', 'state': 'success'}]
        snap = self.client.snapshot()
        self.assertEqual([1, 2], [x['number'] for x in snap['issues']])
        self.assertEqual(2, len(snap['issues'][0]['comments']))
        self.assertEqual('abc', snap['prs'][0]['head']['sha'])
        self.assertEqual('base', snap['base_sha'])
        self.assertEqual('CHANGES_REQUESTED', snap['prs'][0]['reviews'][0]['state'])
        self.assertEqual('success', snap['prs'][0]['checks'][0]['status'])

    def test_raw_file_reads_text_via_contents_api(self):
        self.routes[('GET', '/contents/.code-loop/verify.json', 0)] = ('raw', '{"verification": []}')
        body = self.client.raw_file('.code-loop/verify.json', 'a' * 40)
        self.assertEqual('{"verification": []}', body)
        self.assertIn('ref=' + 'a' * 40, self.queries[-1])

    def test_raw_file_reads_base64_json_via_contents_api(self):
        encoded = base64.b64encode(b'{"key": "value"}').decode('utf-8')
        json_payload = {'encoding': 'base64', 'content': encoded}
        self.routes[('GET', '/contents/.code-loop/verify.json', 0)] = json_payload
        body = self.client.raw_file('.code-loop/verify.json', 'b' * 40)
        self.assertEqual('{"key": "value"}', body)

    def test_raw_file_fallback_to_raw_endpoint(self):
        # 404 on /contents/ triggers fallback to /raw/
        self.routes[('GET', '/contents/.code-loop/verify.json', 0)] = 'missing'
        self.routes[('GET', '/raw/.code-loop/verify.json', 0)] = ('raw', 'raw content')
        body = self.client.raw_file('.code-loop/verify.json', 'c' * 40)
        self.assertEqual('raw content', body)

    def test_raw_file_missing_is_none_while_other_failures_raise(self):
        self.routes[('GET', '/contents/.code-loop/verify.json', 0)] = 'missing'
        self.routes[('GET', '/raw/.code-loop/verify.json', 0)] = 'missing'
        self.assertIsNone(self.client.raw_file('.code-loop/verify.json', 'a' * 40))
        self.routes[('GET', '/contents/.code-loop/verify.json', 0)] = 'error'
        with self.assertRaises(GitHubError):
            self.client.raw_file('.code-loop/verify.json', 'a' * 40)

    def test_raw_file_refuses_branch_names_and_escaping_paths(self):
        for ref in ('main', 'refs/heads/main', 'a' * 39, 'a' * 41, '', None):
            with self.assertRaisesRegex(GitHubError, 'EXAM_REF_INVALID'):
                self.client.raw_file('.code-loop/verify.json', ref)
        for path in ('/etc/passwd', '../x', 'a//b', '', None):
            with self.assertRaisesRegex(GitHubError, 'repo-relative'):
                self.client.raw_file(path, 'a' * 40)

    def test_raw_file_rejects_an_oversize_body_without_reading_it_all(self):
        self.routes[('GET', '/contents/.code-loop/verify.json', 0)] = ('raw', 'x' * (runtime.EXAM_MAX_BYTES + 10))
        with self.assertRaisesRegex(GitHubError, 'EXAM_INVALID: raw file exceeds'):
            self.client.raw_file('.code-loop/verify.json', 'a' * 40)

    def test_pr_files_lists_changed_paths_across_pages(self):
        self.routes[('GET', '/pulls/3/files', 1)] = [{'filename': '.code-loop/verify.json'}]
        self.routes[('GET', '/pulls/3/files', 2)] = [{'filename': 'src/x.py'}]
        self.assertEqual(['.code-loop/verify.json', 'src/x.py'], self.client.pr_files(3))
        self.assertEqual('/pulls/3/files', self.calls[-1][1])

    def test_pr_files_rejects_entries_without_filename(self):
        self.routes[('GET', '/pulls/3/files', 1)] = [{'filepath': 'x'}]
        with self.assertRaises(GitHubError):
            self.client.pr_files(3)

    def test_pr_files_failures_raise_for_a_clean_retry(self):
        self.routes[('GET', '/pulls/3/files', 1)] = 'error'
        with self.assertRaises(GitHubError):
            self.client.pr_files(3)

    def test_issue_rows_with_pull_request_filtered_out(self):
        self.routes[('GET', '/issues', 1)] = [
            issue(1, pull_request=None),
            issue(2, pull_request={'url': 'https://api.github.com/repos/a/b/pulls/2'}),
        ]
        snap = self.client.snapshot()
        self.assertEqual([1], [x['number'] for x in snap['issues']])

    def test_find_create_issue_tolerates_null_pull_request(self):
        self.routes[('GET', '/issues', 1)] = [dict(issue(1), body='marker-x', pull_request=None)]
        self.routes[('GET', '/issues', 2)] = []
        payload = {'title': 't', 'body': 'b'}
        self.assertEqual(1, self.client.find('marker-x', 'create_issue', payload)['number'])

    def test_snapshot_known_skips_unchanged_closed_threads(self):
        closed = dict(issue(1), state='closed', updated_at='2026-09-01T00:00:00Z')
        self.routes[('GET', '/issues', 1)] = [closed]
        snap = self.client.snapshot({('issue', 1): ('closed', '2026-09-01T00:00:00Z')})
        self.assertEqual([], snap['issues'][0]['comments'])
        self.assertFalse(any(p == '/issues/1/comments' for _, p, _, _ in self.calls))

        self.routes[('GET', '/issues', 1)] = [issue(2)]
        self.routes[('GET', '/issues/2/comments', 1)] = [{'id': 5, 'body': 'x'}]
        snap = self.client.snapshot({('issue', 1): ('closed', '2026-09-01T00:00:00Z')})
        self.assertEqual([2], [i['number'] for i in snap['issues']])
        self.assertEqual('x', snap['issues'][0]['comments'][0]['body'])

    def test_snapshot_known_changed_closed_item_refetches_thread(self):
        closed_new = dict(issue(1), state='closed', updated_at='2026-09-02T00:00:00Z')
        self.routes[('GET', '/issues', 1)] = [closed_new]
        self.routes[('GET', '/issues/1/comments', 1)] = [{'id': 7, 'body': 'new'}]
        known = {('issue', 1): ('closed', '2026-09-01T00:00:00Z')}
        snap = self.client.snapshot(known)
        self.assertEqual('new', snap['issues'][0]['comments'][0]['body'])

    def test_snapshot_known_skips_unchanged_closed_pr(self):
        pr_row = dict(issue(3), state='closed', updated_at='2026-09-01T00:00:00Z',
                      head={'sha': 'abc', 'ref': 'f'},
                      base={'sha': 'base', 'ref': 'main'}, merged=True)
        self.routes[('GET', '/pulls', 1)] = [pr_row]
        known = {('pr', 3): ('closed', '2026-09-01T00:00:00Z')}
        snap = self.client.snapshot(known)
        self.assertEqual([], snap['prs'][0]['comments'])
        self.assertEqual([], snap['prs'][0]['reviews'])
        self.assertFalse(any('/pulls/3' in p for _, p, _, _ in self.calls))

    def test_statuses_always_observed_and_page_limit(self):
        pr = issue(3, head={'sha': 'abc', 'ref': 'feature/x'},
                   base={'sha': 'base', 'ref': 'main'}, merged=False)
        self.routes[('GET', '/pulls', 1)] = [pr]
        self.routes[('GET', '/pulls/3', 0)] = pr
        self.routes[('GET', '/commits/abc/statuses', 1)] = [
            {'id': 1, 'context': 'ci', 'state': 'success'}]
        self.assertEqual('success', self.client.snapshot()['prs'][0]['checks'][0]['status'])
        self.client.max_pages = 1
        with self.assertRaises(GitHubError):
            self.client.snapshot()

    def test_write_receipt_and_patch_allowlist(self):
        self.routes[('POST', '/issues', 0)] = {'number': 8}
        self.assertEqual(8, self.client.write('create_issue', {'title': 'x', 'body': ''})['number'])
        self.routes[('PATCH', '/issues/8', 0)] = dict(issue(8), state='closed')
        self.client.write('close_issue', {'number': 8})
        self.assertEqual({'state': 'closed'}, json.loads(self.calls[-1][3]))
        self.routes[('POST', '/issues', 0)] = {}
        with self.assertRaises(GitHubError):
            self.client.write('create_issue', {'title': 'x', 'body': ''})

    def test_close_issue_with_evidence_comment_then_state_patch(self):
        self.routes[('POST', '/issues/8/comments', 0)] = {'id': 3}
        self.routes[('PATCH', '/issues/8', 0)] = dict(issue(8), state='closed')
        self.client.write('close_issue', {'number': 8, 'body': 'evidence'})
        post = next(c for c in self.calls if c[0] == 'POST')
        self.assertEqual('/issues/8/comments', post[1])
        self.assertEqual({'state': 'closed'}, json.loads(self.calls[-1][3]))

    def test_close_issue_find_needs_closed_state_and_marker_comment(self):
        self.routes[('GET', '/issues/8', 0)] = issue(8)
        self.assertIsNone(self.client.find('m', 'close_issue', {'number': 8, 'body': 'x'}))
        self.routes[('GET', '/issues/8', 0)] = dict(issue(8), state='closed')
        self.routes[('GET', '/issues/8/comments', 1)] = [{'id': 1, 'body': 'unrelated'}]
        self.assertIsNone(self.client.find('m', 'close_issue', {'number': 8, 'body': 'x'}))
        self.routes[('GET', '/issues/8/comments', 1)] = [{'id': 2, 'body': 'ev m'}]
        self.assertEqual(8, self.client.find('m', 'close_issue', {'number': 8, 'body': 'x'})['number'])
        self.assertEqual(8, self.client.find('m', 'close_issue', {'number': 8})['number'])

    def test_recovery_fails_on_incomplete_scan_or_duplicate_marker(self):
        self.routes[('GET', '/issues', 1)] = [dict(issue(1), body='marker')]
        self.routes[('GET', '/issues', 2)] = 'error'
        with self.assertRaises(GitHubError):
            self.client.find('marker', 'create_issue', {})
        self.routes[('GET', '/issues', 2)] = [dict(issue(2), body='marker')]
        with self.assertRaises(GitHubError):
            self.client.find('marker', 'create_issue', {})

    def test_later_page_failure(self):
        self.routes[('GET', '/issues', 1)] = [issue(1)]
        self.routes[('GET', '/issues', 2)] = 'error'
        with self.assertRaises(GitHubError):
            self.client.snapshot()

    def test_repeated_page_accepted_as_complete(self):
        self.routes[('GET', '/issues', 1)] = [issue(1)]
        self.routes[('GET', '/issues', 2)] = [issue(1)]
        snapshot = self.client.snapshot()
        self.assertEqual([1], [x['number'] for x in snapshot['issues']])

    def test_non_paginating_endpoint_returns_complete_list(self):
        for total in (69, 50):
            client = GitHub({'url': 'http://127.0.0.1:%d' % self.server.server_port,
                             'repo': 'a/b', 'page_limit': 50})
            comments = [{'id': i, 'body': 'c%d' % i} for i in range(1, total + 1)]
            self.routes[('GET', '/issues', 1)] = [issue(9)]
            self.routes[('GET', '/issues/9/comments', 1)] = comments
            self.routes[('GET', '/issues/9/comments', 2)] = comments
            snapshot = client.snapshot()
            got = snapshot['issues'][0]['comments']
            self.assertEqual(total, len(got))
            self.assertEqual(total, len({c['id'] for c in got}))

    def test_oscillating_pages_still_rejected(self):
        self.routes[('GET', '/issues', 1)] = [issue(1)]
        self.routes[('GET', '/issues', 2)] = [issue(2)]
        self.routes[('GET', '/issues', 3)] = [issue(1)]
        with self.assertRaisesRegex(GitHubError, 'Repeated GitHub page'):
            self.client.snapshot()

    def test_full_page_repeated_accepted_as_complete(self):
        client = GitHub({'url': 'http://127.0.0.1:%d' % self.server.server_port,
                         'repo': 'a/b', 'page_limit': 2})
        self.routes[('GET', '/issues', 1)] = [issue(1), issue(2)]
        self.routes[('GET', '/issues', 2)] = [issue(1), issue(2)]
        snapshot = client.snapshot()
        self.assertEqual([1, 2], [x['number'] for x in snapshot['issues']])

    def test_wrong_type_and_missing_fields(self):
        for value in ({}, [None], [{'number': 1}]):
            self.routes[('GET', '/issues', 1)] = value
            with self.assertRaises(GitHubError):
                self.client.snapshot()

    def test_no_merge_or_arbitrary_payload(self):
        for action, payload in [('merge', {'number': 1}),
                                ('create_pr', {'title': 'a', 'body': '', 'head': 'x', 'base': 'main', 'merge': True})]:
            with self.assertRaises(GitHubError):
                self.client.write(action, payload)
        self.assertEqual([], self.calls)

    def test_write_and_recover_after_disconnect(self):
        marker = '<!-- cp:123 -->'
        self.routes[('POST', '/issues', 0)] = 'disconnect'
        with self.assertRaises(GitHubError):
            self.client.write('create_issue', {'title': 'x', 'body': marker})
        self.routes[('GET', '/issues', 1)] = [dict(issue(4), body=marker)]
        receipt = self.client.find(marker, 'create_issue', {'title': 'x', 'body': marker})
        self.assertEqual(4, receipt['number'])
        self.assertEqual(1, sum(x[0] == 'POST' for x in self.calls))

    def test_comments_marker_complete_pages(self):
        self.routes[('GET', '/issues/5/comments', 1)] = [{'id': 1, 'body': 'old'}]
        self.routes[('GET', '/issues/5/comments', 2)] = [{'id': 2, 'body': '<!-- cp:5 -->'}]
        self.assertEqual(2, self.client.find('<!-- cp:5 -->', 'review_comment', {'number': 5})['id'])

    def test_missing_token_rejected_and_tls_not_disabled(self):
        with self.assertRaises(GitHubError):
            GitHub({'url': 'https://api.github.com', 'repo': 'a/b', 'token_env': 'CP_TEST_MISSING_TOKEN'})
        with self.assertRaises(GitHubError):
            GitHub({'url': 'http://example.invalid', 'repo': 'a/b'})

    def test_token_falls_back_to_machine_local_file(self):
        import tempfile
        saved = os.environ.pop('CP_TEST_FILE_TOKEN', None)
        token_file = None
        try:
            with self.assertRaises(GitHubError):
                GitHub({'url': 'https://api.github.com', 'repo': 'a/b', 'token_env': 'CP_TEST_FILE_TOKEN'})
            with tempfile.NamedTemporaryFile('w', suffix='.token', delete=False) as f:
                f.write('  file-token \n')
                token_file = f.name
            client = GitHub({'url': 'https://api.github.com', 'repo': 'a/b',
                             'token_env': 'CP_TEST_FILE_TOKEN', 'token_file': token_file})
            self.assertEqual('file-token', client.token)
        finally:
            if saved is not None:
                os.environ['CP_TEST_FILE_TOKEN'] = saved
            if token_file:
                Path(token_file).unlink(missing_ok=True)

    def test_token_falls_back_to_gitea_env(self):
        saved_gh = os.environ.pop('GITHUB_TOKEN', None)
        saved_gt = os.environ.get('GITEA_TOKEN', None)
        try:
            os.environ['GITEA_TOKEN'] = 'fallback-token'
            client = GitHub({'url': 'https://api.github.com', 'repo': 'a/b', 'token_env': 'GITHUB_TOKEN'})
            self.assertEqual('fallback-token', client.token)
        finally:
            if saved_gh is not None:
                os.environ['GITHUB_TOKEN'] = saved_gh
            if saved_gt is not None:
                os.environ['GITEA_TOKEN'] = saved_gt
            else:
                os.environ.pop('GITEA_TOKEN', None)

    def test_list_issues_light_read(self):
        pr = issue(3, pull_request={'url': 'https://api.github.com/repos/a/b/pulls/3'},
                   head={'sha': 'abc', 'ref': 'f'}, base={'sha': 'b', 'ref': 'main'},
                   merged=False)
        self.routes[('GET', '/issues', 1)] = [issue(1, pull_request=None), pr]
        self.routes[('GET', '/issues', 2)] = []
        rows = self.client.list_issues(state='open')
        self.assertEqual([1], [x['number'] for x in rows])
        self.assertEqual('open', rows[0]['state'])
        self.assertFalse(any('/comments' in p for _, p, _, _ in self.calls))

    def test_list_issues_pagination_contract(self):
        self.routes[('GET', '/issues', 1)] = [issue(1, pull_request=None)]
        self.routes[('GET', '/issues', 2)] = []
        self.assertEqual([1], [x['number'] for x in self.client.list_issues()])
        self.routes[('GET', '/issues', 2)] = [issue(1, pull_request=None)]
        self.assertEqual([1], [x['number'] for x in self.client.list_issues()])

    def test_list_issues_malformed_row_rejected(self):
        self.routes[('GET', '/issues', 1)] = [{'number': 1}]
        with self.assertRaises(GitHubError):
            self.client.list_issues()

    def test_snapshot_pull_request_simple_merged_at(self):
        # A realistic GitHub Pull Request Simple item from GET /pulls has merged_at, but no merged field
        pr_simple = dict(issue(3), state='closed', updated_at='2026-09-01T00:00:00Z',
                         head={'sha': 'abc', 'ref': 'f'},
                         base={'sha': 'base', 'ref': 'main'},
                         merged_at='2026-09-01T00:00:00Z')
        self.routes[('GET', '/pulls', 1)] = [pr_simple]
        known = {('pr', 3): ('closed', '2026-09-01T00:00:00Z')}
        snap = self.client.snapshot(known)
        self.assertTrue(snap['prs'][0]['merged'])

    def test_snapshot_pull_request_simple_unmerged(self):
        pr_simple = dict(issue(4), state='closed', updated_at='2026-09-01T00:00:00Z',
                         head={'sha': 'def', 'ref': 'f'},
                         base={'sha': 'base', 'ref': 'main'},
                         merged_at=None)
        self.routes[('GET', '/pulls', 1)] = [pr_simple]
        known = {('pr', 4): ('closed', '2026-09-01T00:00:00Z')}
        snap = self.client.snapshot(known)
        self.assertFalse(snap['prs'][0]['merged'])

    def test_check_runs_and_actions_statuses(self):
        pr = issue(5, head={'sha': 'sha123', 'ref': 'feature/y'},
                   base={'sha': 'base', 'ref': 'main'}, merged=False)
        self.routes[('GET', '/pulls', 1)] = [pr]
        self.routes[('GET', '/pulls/5', 0)] = pr
        self.routes[('GET', '/commits/sha123/statuses', 1)] = [
            {'id': 1, 'context': 'status-ci', 'state': 'success'}]
        self.routes[('GET', '/commits/sha123/check-runs', 0)] = {
            'total_count': 1,
            'check_runs': [{'id': 20, 'name': 'actions-build', 'status': 'completed', 'conclusion': 'success'}]
        }
        checks = self.client.snapshot()['prs'][0]['checks']
        checks_map = {c['context']: c['status'] for c in checks}
        self.assertEqual('success', checks_map.get('status-ci'))
        self.assertEqual('success', checks_map.get('actions-build'))

    def test_gh_auth_token_fallback(self):
        saved_gh = os.environ.pop('GITHUB_TOKEN', None)
        saved_gt = os.environ.pop('GITEA_TOKEN', None)
        try:
            with mock.patch('shutil.which', return_value='/usr/bin/gh'):
                with mock.patch('subprocess.run') as mock_run:
                    mock_run.return_value = mock.Mock(returncode=0, stdout='mocked-gh-token\n')
                    client = GitHub({'url': 'https://api.github.com', 'repo': 'a/b'})
                    self.assertEqual('mocked-gh-token', client.token)
        finally:
            if saved_gh is not None:
                os.environ['GITHUB_TOKEN'] = saved_gh
            if saved_gt is not None:
                os.environ['GITEA_TOKEN'] = saved_gt


if __name__ == '__main__':
    unittest.main()
