#!/usr/bin/env python3
"""Conflict-resolution and BLOCKED-visibility tests (issue #192).

Split out of test_control_plane_service.py so the suite stays under the
800-line single-file red line; shares the ServiceTestsBase harness.
"""
import json
import subprocess
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts"),
           str(_Path(__file__).resolve().parent)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from _service_test_base import Platform, git, ServiceTestsBase


class ConflictResolutionTests(ServiceTestsBase):
    def test_blocked_conflict_report_requires_visibility_comment(self):
        wt, cfg = self._worktree(origin=True)
        head = git(wt, 'rev-parse', 'HEAD')
        s, p = self._fresh(cfg)
        pr = self._conflict_pr()
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        with self.assertRaisesRegex(ValueError, 'BLOCKED_VISIBILITY_REQUIRED'):
            s.report(e['id'], e['token'], {'decision': 'BLOCKED', 'reviewer': 'verifier',
                        'summary': 'branch occupied by foreign worktree'})
        s.report(e['id'], e['token'], {'decision': 'BLOCKED', 'reviewer': 'verifier',
                    'summary': 'branch occupied', 'action': 'comment_pr',
                    'payload': {'number': 2, 'body': 'conflict files + needed ruling'}})
        s.publish(e['id'], e['token'])
        action, payload, _ = p.writes[-1]
        self.assertEqual('comment_pr', action)
        body = payload['body']
        self.assertIn('Decision: Blocked\nHead: ' + head + '\nBase: base1', body)
        self.assertIn('branch occupied', body)
        self.assertIn('> 🤖 本条由 code-loop **handler** 循环自动生成', body)
        self.assertIn('<!-- code-loop-agent: handler -->', body)
        self.assertIn('<!-- code-loop:', body, 'opcode marker must remain')

    def test_comment_pr_rejected_for_wrong_target_or_decision(self):
        wt, cfg = self._worktree()
        head = git(wt, 'rev-parse', 'HEAD')
        s, p = self._fresh(cfg)
        pr = self._conflict_pr()
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        with self.assertRaisesRegex(ValueError, 'WRONG_PR_TARGET'):
            s.report(e['id'], e['token'], {'decision': 'BLOCKED', 'reviewer': 'verifier',
                        'summary': 'x', 'action': 'comment_pr',
                        'payload': {'number': 999, 'body': 'x'}})
        with self.assertRaisesRegex(ValueError, 'ACTION_DECISION_MISMATCH'):
            s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                        'summary': 'x', 'action': 'comment_pr',
                        'payload': {'number': 2, 'body': 'x'}})

    def test_discover_cannot_use_comment_pr(self):
        self.platform.data['prs'] = [self.pr()]
        self.s.sync()
        e = self.s.claim('discover', 'inspector')
        with self.assertRaisesRegex(ValueError, 'ROLE_ACTION_FORBIDDEN'):
            self.s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'other',
                          'summary': 'evidence', 'action': 'comment_pr',
                          'payload': {'number': 2, 'body': 'bad'}})

    def test_blocked_issue_report_without_action_still_allowed(self):
        self.platform.data['issues'] = [self.issue()]
        self.s.sync()
        item = [x for x in self.s.store.items('handler')
                if x['state'] == 'READY' and 'ISSUE_IMPLEMENTATION' in x['payload']][0]
        e = self.s.claim('handler', 'worker', item_id=item['id'])
        self.s.report(e['id'], e['token'], {'decision': 'BLOCKED', 'reviewer': 'verifier',
                      'summary': 'cannot reproduce'})

    def test_conflict_resolution_temp_branch_binds_and_pushes_pr_ref(self):
        wt, cfg = self._worktree(origin=True)
        head = git(wt, 'rev-parse', 'HEAD')
        wt2 = str(Path(wt).parent / 'repair-pr-2')
        git(Path(cfg['repo_root']), 'worktree', 'add', '-b', 'feature/mocx/repair-pr-2', wt2, head)
        s, p = self._fresh(cfg)
        pr = self._conflict_pr()
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        ctx = s.bind(e['id'], e['token'], wt2, 'feature/mocx/repair-pr-2')
        self.assertEqual('feature/mocx/repair-pr-2', ctx['branch'])
        resources = json.loads(s.store.db.execute(
            'SELECT resources FROM work_items WHERE id=?', (item['id'],)).fetchone()['resources'])
        self.assertIn('branch-feature/mocx/repair-pr-2', resources)
        Path(wt2, 'resolved.txt').write_text('merged\n', encoding='utf-8')
        git(wt2, 'add', '.')
        git(wt2, 'commit', '-m', 'resolve conflict')
        resolved = git(wt2, 'rev-parse', 'HEAD')
        s.verify(e['id'], e['token'])
        s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                    'summary': 'resolved', 'action': 'edit_pr',
                    'payload': {'number': 2, 'title': 'fix', 'body': 'conflict resolved'}})
        s.publish(e['id'], e['token'])
        bare = Path(cfg['repo_root']).parent / 'origin.git'
        self.assertEqual(resolved, git(bare, 'rev-parse', 'refs/heads/fix/mocx/bug'))
        with self.assertRaises(subprocess.CalledProcessError):
            git(bare, 'rev-parse', 'refs/heads/feature/mocx/repair-pr-2')
        # Non-forced refspec: a diverged remote branch rejects an ordinary push.
        with self.assertRaises(subprocess.CalledProcessError):
            git(wt2, 'push', 'origin', resolved + '~1:refs/heads/fix/mocx/bug')
        self.assertEqual(resolved, git(bare, 'rev-parse', 'refs/heads/fix/mocx/bug'))

    def test_conflict_resolution_temp_branch_must_start_at_pr_head(self):
        wt, cfg = self._worktree()
        Path(wt, 'feature.txt').write_text('x\n', encoding='utf-8')
        git(wt, 'add', '.')
        git(wt, 'commit', '-m', 'pr work')
        pr_head = git(wt, 'rev-parse', 'HEAD')
        wt2 = str(Path(wt).parent / 'repair-pr-2')
        git(Path(cfg['repo_root']), 'worktree', 'add', '-b', 'feature/mocx/repair-pr-2', wt2, 'main')
        s, p = self._fresh(cfg)
        pr = self._conflict_pr()
        pr['head'] = {'sha': pr_head, 'ref': 'fix/mocx/bug'}
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        with self.assertRaisesRegex(ValueError, 'PR_HEAD_MISMATCH'):
            s.bind(e['id'], e['token'], wt2, 'feature/mocx/repair-pr-2')

    def test_review_fix_temp_branch_binds_at_pr_head(self):
        """issue #279: 原分支被外部 worktree 占用时，PR_REVIEW_FIX 允许起点=
        审批 head 的临时修复分支。"""
        wt, cfg = self._worktree()
        head = git(wt, 'rev-parse', 'HEAD')
        wt2 = str(Path(wt).parent / 'repair-pr-2')
        git(Path(cfg['repo_root']), 'worktree', 'add', '-b', 'feature/mocx/repair-pr-2', wt2, head)
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        pr['reviews'] = [{'user': {'id': 9}, 'state': 'REQUEST_CHANGES', 'body': 'fix it'}]
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        ctx = s.bind(e['id'], e['token'], wt2, 'feature/mocx/repair-pr-2')
        self.assertEqual('feature/mocx/repair-pr-2', ctx['branch'])
        self.assertEqual(head, ctx['head'])

    def test_comment_pr_receipt_comment_is_not_source_input(self):
        self.s.store.put_item('marker-cpr', 'handler', 'v1', {'task': 'PR_CONFLICT_RESOLUTION'}, ['pr-1'])
        execution = self.s.store.claim('handler', 'handler')
        self.s.store.db.execute("INSERT INTO outbox(id,execution_id,action,payload,state,receipt) VALUES (?,?,?,?,?,?)",
                                ('cpr-op', execution['id'], 'comment_pr', '{}', 'SENT', '{}'))
        comment = {'id': 3, 'body': '<!-- code-loop:cpr-op -->', 'user': {}}
        self.assertEqual([], self.s.source([comment]))

