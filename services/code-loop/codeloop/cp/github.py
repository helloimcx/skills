"""Fail-closed, stdlib GitHub adapter. No retries for writes or merge API.

API contract: GitHub REST API (v2022-11-28). Callers persist snapshots and
outbox intent; an exception on a write means its outcome may be uncertain.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

from .runtime import EXAM_MAX_BYTES


class GitHubError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GitHubError('GitHub redirects are not accepted')


class GitHub:
    ACTION_FIELDS = {
        'create_issue': {'title', 'body'},
        'comment_issue': {'number', 'body'},
        'create_pr': {'title', 'body', 'head', 'base'},
        'edit_pr': {'number', 'title', 'body'},
        'review_comment': {'number', 'body'},
        'comment_pr': {'number', 'body'},
        'close_issue': {'number'},  # base shape; _validate_action conditionally
        # accepts an evidence-comment body for discover's fallback closure.
    }

    def __init__(self, config):
        self.config = dict(config)
        url = config.get('url', 'https://api.github.com').rstrip('/')
        parts = urlsplit(url)
        local = parts.hostname in ('127.0.0.1', 'localhost', '::1')
        if (parts.scheme not in ('http', 'https') or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or (parts.scheme == 'http' and not local)):
            raise GitHubError('Use HTTPS (HTTP is allowed only for loopback fixtures)')
        repo = config['repo'].split('/')
        if len(repo) != 2 or any(not x or x in ('.', '..') for x in repo):
            raise GitHubError('repo must be owner/name')
        owner, repo_name = repo
        quoted_repo = quote(owner, safe='') + '/' + quote(repo_name, safe='')

        if parts.hostname in ('github.com', 'api.github.com'):
            self.root = 'https://api.github.com/repos/' + quoted_repo
        elif local:
            prefix = parts.path.rstrip('/')
            if prefix.endswith('/repos/' + quoted_repo):
                self.root = f"{parts.scheme}://{parts.netloc}{prefix}"
            elif prefix:
                self.root = f"{parts.scheme}://{parts.netloc}{prefix}/repos/{quoted_repo}"
            else:
                self.root = f"{parts.scheme}://{parts.netloc}/repos/{quoted_repo}"
        else:
            base_path = parts.path.rstrip('/') or '/api/v3'
            self.root = f"{parts.scheme}://{parts.netloc}{base_path}/repos/{quoted_repo}"

        token_env = config.get('token_env', 'GITHUB_TOKEN')
        self.token = os.environ.get(token_env, '')
        token_file = config.get('token_file') or str(
            Path.home() / '.code-loop' / 'credentials' / token_env)
        if not self.token and Path(token_file).expanduser().is_file():
            self.token = Path(token_file).expanduser().read_text(encoding='utf-8').strip()
        # Fallback to GITEA_TOKEN if GITHUB_TOKEN not configured
        if not self.token and token_env == 'GITHUB_TOKEN':
            gitea_env = 'GITEA_TOKEN'
            self.token = os.environ.get(gitea_env, '')
            gitea_file = Path.home() / '.code-loop' / 'credentials' / gitea_env
            if not self.token and gitea_file.is_file():
                self.token = gitea_file.read_text(encoding='utf-8').strip()

        if not self.token and not local:
            raise GitHubError('Platform token missing: set %s env or write %s' % (token_env, token_file))

        self.timeout = float(config.get('timeout', 15))
        self.limit = int(config.get('page_limit', 50))
        self.max_pages = int(config.get('max_pages', 1000))
        if self.timeout <= 0 or self.limit < 1 or self.max_pages < 1:
            raise GitHubError('timeout and pagination bounds must be positive')
        context = ssl.create_default_context(cafile=config.get('ca_file'))
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))

    def _request(self, path, method='GET', body=None, query=None, accept='application/vnd.github+json'):
        url = self.root + path
        if query:
            url += '?' + urlencode(query)
        headers = {
            'Accept': accept,
            'User-Agent': 'code-loop',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode('utf-8')
        try:
            with self.opener.open(Request(url, data=data, headers=headers, method=method),
                                  timeout=self.timeout) as response:
                raw_bytes = response.read()
                if not raw_bytes:
                    return {}
                return json.loads(raw_bytes.decode('utf-8'))
        except (URLError, OSError, ValueError, http.client.HTTPException) as exc:
            # Do not expose tokens or server bodies. Never automatically retry writes.
            raise GitHubError('%s %s failed (%s)' % (method, path, type(exc).__name__)) from None

    @staticmethod
    def _object(value, fields=()):
        if not isinstance(value, dict) or any(k not in value for k in fields):
            raise GitHubError('Malformed GitHub object; required fields missing')
        return value

    def _pages(self, path, identity=True, **query):
        values, seen, identities = [], set(), set()
        previous = None
        for page in range(1, self.max_pages + 1):
            items = self._request(path, query=dict(query, page=page, per_page=self.limit, limit=self.limit))
            if not isinstance(items, list):
                raise GitHubError('Expected GitHub list: ' + path)
            if not items:
                return values
            fingerprint = json.dumps(items, sort_keys=True)
            if fingerprint == previous:
                # Adjacent identical pages: the endpoint did not apply pagination.
                return values
            previous = fingerprint
            if fingerprint in seen:
                raise GitHubError('Repeated GitHub page: ' + path)
            seen.add(fingerprint)
            for item in items:
                self._object(item)
                # Identity-less endpoints (PR changed files carry no id) keep
                # the page-level protections but cannot be deduped by id.
                if not identity:
                    values.append(item)
                    continue
                key = item.get('id', item.get('number'))
                if isinstance(key, bool) or not isinstance(key, int) or key <= 0:
                    raise GitHubError('List item lacks positive identity: ' + path)
                if key in identities:
                    raise GitHubError('Duplicate GitHub object across pages: ' + path)
                identities.add(key)
                values.append(item)
        raise GitHubError('GitHub pagination limit reached: ' + path)

    def raw_file(self, path, ref):
        """Repository file content at a pinned commit, or None when absent.

        The ref must be a full commit id: the exam file is read at an observed
        revision, never through a movable branch name, so a PR branch cannot
        smuggle a different exam into its own validation. Only HTTP 404 means
        "not present"; every other failure raises.
        """
        if not isinstance(ref, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', ref):
            raise GitHubError('EXAM_REF_INVALID: raw file ref must be a full commit id')
        parts = path.split('/') if isinstance(path, str) else []
        if not parts or any(p in ('', '.', '..') for p in parts):
            raise GitHubError('EXAM_REF_INVALID: raw file path must be repo-relative')

        # Try GitHub Contents API first, with fallback to /raw/ for fixture routes
        url_contents = self.root + '/contents/' + quote(path, safe='/') + '?' + urlencode({'ref': ref})
        headers = {
            'Accept': 'application/vnd.github.raw+json, application/vnd.github.raw, text/plain',
            'User-Agent': 'code-loop',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token

        try:
            with self.opener.open(Request(url_contents, headers=headers), timeout=self.timeout) as response:
                body_bytes = response.read(EXAM_MAX_BYTES + 1)
        except HTTPError as exc:
            if exc.code == 404:
                # Also try /raw/ in case local test fixtures route through /raw/
                url_raw = self.root + '/raw/' + quote(path, safe='/') + '?' + urlencode({'ref': ref})
                try:
                    with self.opener.open(Request(url_raw, headers=headers), timeout=self.timeout) as raw_resp:
                        body_bytes = raw_resp.read(EXAM_MAX_BYTES + 1)
                except HTTPError as raw_exc:
                    if raw_exc.code == 404:
                        return None
                    raise GitHubError('EXAM_UNAVAILABLE: GET raw file failed (HTTP %s)' % raw_exc.code) from None
                except (URLError, OSError, ValueError, http.client.HTTPException) as raw_exc:
                    raise GitHubError('EXAM_UNAVAILABLE: GET raw file failed (%s)'
                                      % type(raw_exc).__name__) from None
            else:
                raise GitHubError('EXAM_UNAVAILABLE: GET raw file failed (HTTP %s)' % exc.code) from None
        except (URLError, OSError, ValueError, http.client.HTTPException) as exc:
            raise GitHubError('EXAM_UNAVAILABLE: GET raw file failed (%s)'
                              % type(exc).__name__) from None

        if len(body_bytes) > EXAM_MAX_BYTES:
            raise GitHubError('EXAM_INVALID: raw file exceeds %d bytes' % EXAM_MAX_BYTES)

        # Check if the response was JSON with base64 content
        try:
            parsed = json.loads(body_bytes.decode('utf-8'))
            if isinstance(parsed, dict) and parsed.get('encoding') == 'base64' and 'content' in parsed:
                return base64.b64decode(parsed['content']).decode('utf-8')
            if isinstance(parsed, str):
                return parsed
        except (ValueError, UnicodeDecodeError):
            pass

        return body_bytes.decode('utf-8')

    def pr_files(self, number):
        """Changed-file paths of a pull request, paginated (exam guard).

        Changed-file entries carry no id, so pagination runs without
        the identity dedup; page-level repetition limits still apply.
        """
        return [self._object(item, ('filename',))['filename']
                for item in self._pages('/pulls/%d/files' % number, identity=False)]

    def _issue(self, item):
        self._object(item, ('number', 'state', 'title', 'body'))
        if (type(item['number']) is not int or item['number'] <= 0
                or item['state'] not in ('open', 'closed')
                or not isinstance(item['title'], str)
                or (item['body'] is not None and not isinstance(item['body'], str))):
            raise GitHubError('Malformed issue fields')
        return item

    def _pr(self, item):
        self._issue(item)
        self._object(item, ('head', 'base', 'merged'))
        if type(item['merged']) is not bool:
            raise GitHubError('Malformed PR merged flag')
        for field in ('head', 'base'):
            ref = self._object(item[field], ('sha', 'ref'))
            if any(not isinstance(ref[k], str) or not ref[k] for k in ('sha', 'ref')):
                raise GitHubError('Malformed PR reference')
        return item

    def _comments(self, number):
        comments = self._pages('/issues/%d/comments' % number)
        for comment in comments:
            self._object(comment, ('body',))
            if not isinstance(comment['body'], str):
                raise GitHubError('Malformed comment body')
        return comments

    def snapshot(self, known=None):
        """Full snapshot of base head, issues and pulls (with threads/reviews).

        `known` maps (kind, number) -> (state, updated_at) of already-stored
        items. A closed item whose state and updated_at are unchanged cannot
        have moved its thread, so the comment/review fetches are skipped and
        empty threads are returned.
        """
        known = known or {}
        branch = self._object(self._request('/branches/' + quote(self.config.get('base', 'main'), safe='')), ('commit',))
        commit = self._object(branch['commit'])
        commit_id = commit.get('sha') or commit.get('id')
        if not isinstance(commit_id, str) or not commit_id:
            raise GitHubError('Missing base commit')

        def thread_stable(kind, number, item):
            prev = known.get((kind, number))
            return (prev is not None and item['state'] == 'closed'
                    and prev[0] == item['state']
                    and prev[1] == item.get('updated_at'))

        issues = []
        for raw in self._pages('/issues', state='all'):
            # GitHub returns PRs in /issues list with a pull_request key.
            # Plain issues lack pull_request or have pull_request=null.
            if raw.get('pull_request'):
                continue
            item = self._issue(raw)
            item['comments'] = ([] if thread_stable('issue', item['number'], item)
                                else self._comments(item['number']))
            issues.append(item)

        prs = []
        for raw in self._pages('/pulls', state='all'):
            number = self._issue(raw)['number']
            if thread_stable('pr', number, raw):
                item = self._pr(raw)
                item['comments'] = []
                item['reviews'] = []
                item['checks'] = []
                prs.append(item)
                continue
            item = self._pr(self._request('/pulls/%d' % number))
            if item['number'] != number:
                raise GitHubError('PR detail identity mismatch')
            item['comments'] = self._comments(number)
            item['reviews'] = self._pages('/pulls/%d/reviews' % number)
            for review in item['reviews']:
                self._object(review, ('state', 'body'))
                if review.get('body') is None:
                    review['body'] = ''
            # Statuses are platform facts and always observed:
            item['checks'] = self._pages('/commits/' + quote(item['head']['sha'], safe='') + '/statuses')
            for check in item['checks']:
                if 'status' not in check and 'state' in check:
                    check['status'] = check['state']
                self._object(check, ('context', 'status'))
            prs.append(item)
        return {'issues': issues, 'prs': prs, 'base_sha': commit_id}

    def list_issues(self, state='open'):
        """Light issue probe for the trigger layer: no comment threads fetched."""
        rows = []
        for raw in self._pages('/issues', state=state):
            if raw.get('pull_request'):
                continue
            rows.append(self._issue(raw))
        return rows

    def _validate_action(self, action, payload):
        fields = self.ACTION_FIELDS.get(action)
        if action == 'close_issue':
            # The close may carry an optional evidence comment (discover's
            # fallback closure); the bare handler shape stays valid.
            fields = {'number', 'body'} if isinstance(payload, dict) and 'body' in payload else {'number'}
        if fields is None or not isinstance(payload, dict) or set(payload) != fields:
            raise GitHubError('Unsupported action or invalid payload fields')
        for key, value in payload.items():
            if key == 'number':
                if type(value) is not int or value <= 0:
                    raise GitHubError('number must be positive integer')
            elif not isinstance(value, str) or (key != 'body' and not value):
                raise GitHubError('Invalid action text field: ' + key)

    def validate_action(self, action, payload):
        """Validate a write before any push or outbox send is attempted."""
        self._validate_action(action, payload)

    def write(self, action, payload):
        self._validate_action(action, payload)
        number = payload.get('number')
        data = {k: v for k, v in payload.items() if k != 'number'}
        if action == 'create_issue':
            path, method = '/issues', 'POST'
        elif action == 'create_pr':
            path, method = '/pulls', 'POST'
        elif action == 'edit_pr':
            path, method = '/pulls/%d' % number, 'PATCH'
        elif action == 'close_issue':
            if payload.get('body'):
                # Evidence comment first (it carries the idempotency marker),
                # then the state PATCH: a lost close is recoverable, a lost
                # comment is not.
                self._request('/issues/%d/comments' % number, 'POST', {'body': payload['body']})
            path, method, data = '/issues/%d' % number, 'PATCH', {'state': 'closed'}
        else:
            path, method = '/issues/%d/comments' % number, 'POST'
        result = self._object(self._request(path, method, data))
        identity = result.get('id', result.get('number'))
        if type(identity) is not int or identity <= 0:
            raise GitHubError('Write receipt lacks identity; outcome uncertain')
        return result

    def find(self, marker, action, payload):
        if action not in self.ACTION_FIELDS or not isinstance(marker, str) or not marker:
            raise GitHubError('Valid action and nonempty marker required')
        if action in ('comment_issue', 'review_comment', 'comment_pr'):
            number = payload.get('number')
            if type(number) is not int or number <= 0:
                raise GitHubError('number must be positive integer')
            objects = self._comments(number)
        elif action == 'close_issue':
            # Closing is a state-idempotent PATCH, recovery verifies the state;
            # the evidence-comment variant additionally requires the marker
            # comment, otherwise the publication is incomplete.
            number = payload.get('number')
            if type(number) is not int or number <= 0:
                raise GitHubError('number must be positive integer')
            obj = self._issue(self._request('/issues/%d' % number))
            if obj['state'] != 'closed':
                return None
            if not payload.get('body'):
                return obj
            comments = self._comments(number)
            return obj if any(marker in c['body'] for c in comments) else None
        else:
            path = '/issues' if action == 'create_issue' else '/pulls'
            objects = self._pages(path, state='all')
        matches = []
        for obj in objects:
            if action == 'create_issue' and obj.get('pull_request'):
                continue
            if action == 'edit_pr' and obj.get('number') != payload.get('number'):
                continue
            self._object(obj, ('body',))
            body = obj['body']
            if body is not None and not isinstance(body, str):
                raise GitHubError('Malformed recovery body')
            if marker in (body or ''):
                matches.append(obj)
        if len(matches) > 1:
            raise GitHubError('Multiple remote objects match operation marker')
        return matches[0] if matches else None
