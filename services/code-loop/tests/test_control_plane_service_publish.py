#!/usr/bin/env python3
import copy
import json
import os
import socket
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts"),
           str(_Path(__file__).resolve().parent)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import EXAM_PATH
from codeloop.cp.exam import GUARD_NOTE as EXAM_GUARD_NOTE
from codeloop.cp.service import Service
from _service_test_base import Platform, git, ServiceTestsBase

class ServicePublishTests(ServiceTestsBase):
    def _repair_events(self, service):
        rows = service.store.db.execute(
            "SELECT id,payload FROM events WHERE kind='pr.handler_repair_needed' "
            'ORDER BY id').fetchall()
        return [(row['id'], json.loads(row['payload'])) for row in rows]

    def test_new_review_and_conflict_edges_coalesce_and_replay_once(self):
        s, p = self._fresh({})
        p.data['prs'] = [self.pr()]
        s.sync()  # Existing state at first observation is not backfilled.
        self.assertEqual([], self._repair_events(s))

        changed = self.pr()
        changed['reviews'] = [{'id': 77, 'user': {'id': 9},
                               'state': 'REQUEST_CHANGES',
                               'body': 'private review text'}]
        changed['mergeable'] = False
        p.data['prs'] = [changed]
        self.expire_observer()
        s.sync()
        events = self._repair_events(s)
        self.assertEqual(1, len(events))
        payload = events[0][1]
        self.assertEqual('pr.handler_repair_needed', payload['kind'])
        self.assertEqual(2, payload['number'])
        self.assertEqual(['changes_requested', 'merge_conflict'],
                         payload['reasons'])
        self.assertIn('github-review:77', payload['source_ids'])
        self.assertNotIn('private review text', json.dumps(payload))
        item = next(row for row in s.store.items('handler')
                    if row['key'] == 'repair:2')
        self.assertEqual(['changes_requested', 'merge_conflict'],
                         json.loads(item['payload'])['repair_reasons'])

        self.expire_observer()
        s.sync()
        self.assertEqual(1, len(self._repair_events(s)))

    def test_new_conflict_edge_without_review_creates_conflict_task(self):
        s, p = self._fresh({})
        p.data['prs'] = [self.pr()]
        s.sync()
        changed = self.pr()
        changed['mergeable'] = False
        p.data['prs'] = [changed]
        self.expire_observer()
        s.sync()
        events = self._repair_events(s)
        self.assertEqual(1, len(events))
        self.assertEqual(['merge_conflict'], events[0][1]['reasons'])
        item = next(row for row in s.store.items('handler')
                    if row['key'] == 'repair:2')
        payload = json.loads(item['payload'])
        self.assertEqual('PR_CONFLICT_RESOLUTION', payload['task'])
        self.assertEqual(['merge_conflict'], payload['repair_reasons'])

    def test_persistent_conflict_on_new_head_creates_fresh_repair_event(self):
        s, p = self._fresh({})
        initial = self.pr()
        p.data['prs'] = [initial]
        s.sync()

        conflicted = self.pr()
        conflicted['mergeable'] = False
        p.data['prs'] = [conflicted]
        self.expire_observer()
        s.sync()
        self.assertEqual(1, len(self._repair_events(s)))

        updated = self.pr()
        updated['head'] = {'sha': 'head-after-push', 'ref': 'feature/fix'}
        updated['mergeable'] = False
        p.data['prs'] = [updated]
        self.expire_observer()
        s.sync()

        events = self._repair_events(s)
        self.assertEqual(2, len(events))
        self.assertEqual('head-after-push', events[-1][1]['head_sha'])
        self.assertEqual(['merge_conflict'], events[-1][1]['reasons'])

    def test_validation_failed_outbox_does_not_create_auto_repair_event(self):
        s, p = self._fresh({})
        pr = self.pr()
        p.data['prs'] = [pr]
        s.sync()
        item = next(row for row in s.store.items('reviewer')
                    if row['state'] == 'READY')
        execution = s.claim('reviewer', 'reviewer', item_id=item['id'])
        s.report(execution['id'], execution['token'], {
            'decision': 'Validation failed', 'reviewer': 'reviewer-agent',
            'summary': 'checks could not be verified', 'action': 'review_comment',
            'payload': {'number': 2, 'body': 'cannot validate'}})
        s.publish(execution['id'], execution['token'])
        s.sync()
        self.assertEqual([], self._repair_events(s))

    def test_sent_reviewer_changes_requested_verdict_creates_one_event(self):
        s, p = self._fresh({})
        p.data['prs'] = [self.pr()]
        s.sync()
        item = next(row for row in s.store.items('reviewer')
                    if row['state'] == 'READY')
        execution = s.claim('reviewer', 'reviewer', item_id=item['id'])
        s.report(execution['id'], execution['token'], {
            'decision': 'Changes requested', 'reviewer': 'reviewer-agent',
            'summary': 'needs a correction', 'action': 'review_comment',
            'payload': {'number': 2, 'body': 'please correct the result'}})
        s.publish(execution['id'], execution['token'])
        self.expire_observer()
        s.sync()
        events = self._repair_events(s)
        self.assertEqual(1, len(events))
        self.assertEqual(['changes_requested'], events[0][1]['reasons'])
        self.assertTrue(any(source.startswith('reviewer-verdict:')
                            for source in events[0][1]['source_ids']))
        self.expire_observer()
        s.sync()
        self.assertEqual(1, len(self._repair_events(s)))

    def test_uncertain_publish_recovers_without_duplicate(self):
        self.s.sync()
        e = self.s.claim('discover', 'inspector')
        self.s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                      'summary': 'independent reproduction', 'action': 'create_issue',
                      'payload': {'title': '[Content] bug', 'body': 'evidence'}})
        self.platform.uncertain = True
        with self.assertRaises(ValueError):
            self.s.publish(e['id'], e['token'])
        self.platform.uncertain = False
        result = self.s.publish(e['id'], e['token'])
        self.assertEqual(result['number'], 100)
        self.assertEqual(len(self.platform.writes), 1)

    def test_publication_payload_is_rejected_before_outbox(self):
        self.s.sync()
        execution = self.s.claim('discover', 'inspector')
        with self.assertRaisesRegex(ValueError, 'INVALID_PUBLICATION_PAYLOAD'):
            self.s.report(execution['id'], execution['token'], {
                'decision': 'CONFIRMED', 'reviewer': 'verifier', 'summary': 'proof',
                'action': 'create_issue', 'payload': {'body': 'missing title'}})
        self.assertEqual(self.s.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    def test_publish_adds_identity_signature_for_each_kind(self):
        # discover: create_issue (no verification gate)
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        p.data['prs'] = []
        s.sync()
        item = [x for x in s.store.items('discover') if x['state'] == 'READY'][0]
        e = s.claim('discover', 'worker', item_id=item['id'])
        s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                      'summary': 'proof', 'action': 'create_issue',
                      'payload': {'title': '[Content] bug', 'body': 'evidence'}})
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('> 🤖 本条由 code-loop **discover** 循环自动生成', body)
        self.assertIn('<!-- code-loop-agent: discover -->', body)
        self.assertIn('<!-- code-loop:', body, 'opcode marker must remain')
        # discover: comment_issue (appending evidence to an existing Issue)
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        p.data['prs'] = []
        s.sync()
        item = [x for x in s.store.items('discover') if x['state'] == 'READY'][0]
        e = s.claim('discover', 'worker', item_id=item['id'])
        s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                      'summary': 'proof', 'action': 'comment_issue',
                      'payload': {'number': 1, 'body': 'new evidence'}})
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('> 🤖 本条由 code-loop **discover** 循环自动生成', body)
        self.assertIn('<!-- code-loop-agent: discover -->', body)
        # reviewer: review_comment with a non-approve verdict skips verification
        s, p = self._fresh({})
        pr = self.pr()
        pr['body'] = 'fix'  # no Closes link, so the review holds no issue lock
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e = s.claim('reviewer', 'worker', item_id=item['id'])
        s.report(e['id'], e['token'], {'decision': 'Changes requested', 'reviewer': 'verifier',
                      'summary': 'blocking issue', 'action': 'review_comment',
                      'payload': {'number': 2, 'body': 'review notes'}})
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('> 🤖 本条由 code-loop **reviewer** 循环自动生成', body)
        self.assertIn('<!-- code-loop-agent: reviewer -->', body)
        self.assertIn('Decision: Changes', body)
        # handler: edit_pr on a PR_REVIEW_FIX task (needs a real origin remote)
        wt, cfg = self._worktree(origin=True)
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['reviews'] = [{'user': {'id': 9}, 'state': 'REQUEST_CHANGES', 'body': 'fix it'}]
        p.data['issues'] = [self.issue()]
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                      'summary': 'fixed', 'action': 'edit_pr',
                      'payload': {'number': 2, 'title': 'fix', 'body': 'fixed per review'}})
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('> 🤖 本条由 code-loop **handler** 循环自动生成', body)
        self.assertIn('<!-- code-loop-agent: handler -->', body)
        # handler: create_pr on an ISSUE_IMPLEMENTATION task
        wt, cfg = self._worktree(origin=True)
        s, p = self._fresh(cfg)
        p.data['issues'] = [self.issue()]
        p.data['prs'] = []
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                      'summary': 'implemented', 'action': 'create_pr',
                      'payload': {'title': 'fix bug', 'body': 'Closes #1\n\nfix',
                                  'head': 'fix/mocx/bug', 'base': 'main'}})
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('> 🤖 本条由 code-loop **handler** 循环自动生成', body)
        self.assertIn('<!-- code-loop-agent: handler -->', body)

    def _conflict_pr(self):
        """PR #2 mergeable=false → PR_CONFLICT_RESOLUTION repair item."""
        pr = self.pr()
        pr['mergeable'] = False
        return pr

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

    def test_blocked_comment_pr_stamps_exam_guard(self):
        """AC#9 BLOCKED 分支：冲突可见性评论与判词走同一护栏。"""
        wt, cfg = self._worktree(origin=True)
        head = git(wt, 'rev-parse', 'HEAD')
        s, p = self._fresh(cfg)
        pr = self._conflict_pr()
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        p.data['prs'] = [pr]
        p.changed_files = [EXAM_PATH]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        s.report(e['id'], e['token'], {'decision': 'BLOCKED', 'reviewer': 'verifier',
                    'summary': 'branch occupied', 'action': 'comment_pr',
                    'payload': {'number': 2, 'body': 'conflict files + needed ruling'}})
        s.publish(e['id'], e['token'])
        self.assertIn(EXAM_GUARD_NOTE, p.writes[-1][1]['body'])

    def test_create_pr_publication_is_never_stamped(self):
        """spec B5：handler create_pr 不盖章——其 PR 后续必经 reviewer 轮，
        即使 diff 触碰考题路径也不加警示行、不记事件。"""
        wt, cfg = self._worktree(origin=True)
        s, p = self._fresh(cfg)
        p.data['issues'] = [self.issue()]
        p.data['prs'] = []
        p.changed_files = [EXAM_PATH]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                      'summary': 'implemented', 'action': 'create_pr',
                      'payload': {'title': 'fix bug', 'body': 'Closes #1\n\nfix',
                                  'head': 'fix/mocx/bug', 'base': 'main'}})
        s.publish(e['id'], e['token'])
        self.assertNotIn(EXAM_GUARD_NOTE, p.writes[-1][1]['body'])
        self.assertEqual([], s.store.db.execute(
            "SELECT 1 FROM events WHERE kind='exam.touched_by_pr'").fetchall())

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
        审批 head 的临时修复分支——publish 按 refspec 推回原 PR 分支，不依赖
        本地分支名，强制原分支检出只会死锁。"""
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

    def test_review_fix_temp_branch_must_start_at_pr_head(self):
        """head 不匹配仍拒：临时修复分支起点非审批 head 时，PR_REVIEW_FIX
        与冲突解决路径同样拒绝 PR_HEAD_MISMATCH，绝不静默锚错。"""
        wt, cfg = self._worktree()
        Path(wt, 'feature.txt').write_text('x\n', encoding='utf-8')
        git(wt, 'add', '.')
        git(wt, 'commit', '-m', 'pr work')
        pr_head = git(wt, 'rev-parse', 'HEAD')
        wt2 = str(Path(wt).parent / 'repair-pr-2')
        git(Path(cfg['repo_root']), 'worktree', 'add', '-b', 'feature/mocx/repair-pr-2', wt2, 'main')
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['head'] = {'sha': pr_head, 'ref': 'fix/mocx/bug'}
        pr['reviews'] = [{'user': {'id': 9}, 'state': 'REQUEST_CHANGES', 'body': 'fix it'}]
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        with self.assertRaisesRegex(ValueError, 'PR_HEAD_MISMATCH'):
            s.bind(e['id'], e['token'], wt2, 'feature/mocx/repair-pr-2')

    def test_review_fix_temp_branch_binds_and_pushes_pr_ref(self):
        """issue #279 端到端：原分支被占用时 PR_REVIEW_FIX 在临时分支修复，
        publish 必须把 commit 推到 PR 原分支 ref（remap 含 PR_REVIEW_FIX），
        临时分支 ref 不得残留——修的是「修复 commit 落错 ref」的 BLOCKER。"""
        wt, cfg = self._worktree(origin=True)
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
        Path(wt2, 'fix.txt').write_text('review fix\n', encoding='utf-8')
        git(wt2, 'add', '.')
        git(wt2, 'commit', '-m', 'address review')
        fixed = git(wt2, 'rev-parse', 'HEAD')
        s.verify(e['id'], e['token'])
        s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                    'summary': 'review fixes applied', 'action': 'edit_pr',
                    'payload': {'number': 2, 'title': 'fix', 'body': 'review applied'}})
        s.publish(e['id'], e['token'])
        bare = Path(cfg['repo_root']).parent / 'origin.git'
        # 修复 commit 落在 PR 原分支 ref，临时分支 ref 不残留。
        self.assertEqual(fixed, git(bare, 'rev-parse', 'refs/heads/fix/mocx/bug'))
        with self.assertRaises(subprocess.CalledProcessError):
            git(bare, 'rev-parse', 'refs/heads/feature/mocx/repair-pr-2')

    def test_comment_pr_receipt_comment_is_not_source_input(self):
        self.s.store.put_item('marker-cpr', 'handler', 'v1', {'task': 'PR_CONFLICT_RESOLUTION'}, ['pr-1'])
        execution = self.s.store.claim('handler', 'handler')
        self.s.store.db.execute("INSERT INTO outbox(id,execution_id,action,payload,state,receipt) VALUES (?,?,?,?,?,?)",
                                ('cpr-op', execution['id'], 'comment_pr', '{}', 'SENT', '{}'))
        comment = {'id': 3, 'body': '<!-- code-loop:cpr-op -->', 'user': {}}
        self.assertEqual([], self.s.source([comment]))


if __name__ == '__main__':
    unittest.main()
