#!/usr/bin/env python3
"""repair_round on the worker agent, end to end: real ledger, real git, real
HTTP over loopback — the repair flavor of AgentEndToEndTests (harness reused;
the spawned session stays the only fake). Pins the three shapes an operator
click can produce: free original branch, occupied original branch (temp
repair branch), and an unclaimable target."""
import time
from pathlib import Path

import test_cp_agent
from test_cp_agent import AgentEndToEndTests


class RepairEndToEndTests(AgentEndToEndTests):
    """execute({'action': 'repair_round'}) claims repair:<n> on the handler
    queue, cuts a branch worktree, binds with the branch and spawns the
    repair session."""

    def setUp(self):
        super().setUp()
        # the repair prompt points at the handler doc — sibling of the
        # registered rules doc, exactly like production's reviewer.md/handler.md
        (self.cfg_dir / 'handler.md').write_text('handler loop doc',
                                                 encoding='utf-8')
        self.base_sha = self.git(self.repo, 'rev-parse', 'main')

    def seed_repair(self, number=88):
        service = self.coordinator.service_for('a__b')
        try:
            service.store.put_item(
                'repair:%d' % number, 'handler', 'v1',
                {'task': 'PR_CONFLICT_RESOLUTION', 'base_sha': self.base_sha,
                 'pr': {'number': number, 'title': 'PR %d' % number,
                        'html_url': 'http://g/pr/%d' % number,
                        'head': {'ref': 'feature/mocx/pr88', 'sha': self.head_sha},
                        'base': {'ref': 'main'}}},
                ['pr-%d' % number, 'branch-feature/mocx/pr88', 'path:*'])
            service.set_meta('synced_at', time.time())
            service.set_meta('current_versions', {'repair:%d' % number: 'v1'})
            row, _ = service.enqueue_command('repair_round', number)
            service.store.mark_dispatched(row['id'], 'agent-box')
            return service, service.latest_command('repair_round', number)
        finally:
            service.store.db.close()

    def test_full_repair_path_claims_binds_branch_and_spawns(self):
        service, row = self.seed_repair()
        self.agent.execute({'id': row['id'], 'action': 'repair_round',
                            'number': 88, 'project': 'a__b'})
        service = self.coordinator.service_for('a__b')
        try:
            self.assertEqual('SPAWNED',
                             service.latest_command('repair_round', 88)['state'])
            execution = service.store.db.execute('SELECT * FROM executions').fetchone()
            worktree = str(self.worktrees / 'repair-pr-88')
            self.assertEqual('RUNNING', execution['state'])
            # original PR branch: free and exactly at the platform head
            self.assertEqual('feature/mocx/pr88', execution['branch'])
            self.assertEqual(worktree, execution['worktree'])
            self.assertEqual(self.head_sha,
                             self.git(worktree, 'rev-parse', 'HEAD'))
            prompt = (self.cfg_dir / 'state'
                      / ('trigger-prompt-%s.txt' % execution['id'])).read_text(
                encoding='utf-8')
            self.assertIn('repair:88', prompt)
            self.assertIn('分支 feature/mocx/pr88，已 bind', prompt)
            self.assertIn('非 force 推回原 PR 分支 feature/mocx/pr88', prompt)
            self.assertIn('action: "edit_pr"', prompt)
            self.assertIn(str(self.cfg_dir / 'handler.md'), prompt)
            self.assertIn('不要 claim、不要 bind、不要另建 worktree', prompt)
            self.assertEqual(1, len(self.spawned))
            self.assertEqual(self.repo, Path(self.spawned[0]['cwd']))
        finally:
            service.store.db.close()

    def test_occupied_branch_prepares_the_temp_repair_branch(self):
        foreign = self.root / 'foreign-wt'
        self.git(self.repo, 'worktree', 'add', str(foreign), 'feature/mocx/pr88')
        try:
            service, row = self.seed_repair()
            self.agent.execute({'id': row['id'], 'action': 'repair_round',
                                'number': 88, 'project': 'a__b'})
            service = self.coordinator.service_for('a__b')
            try:
                execution = service.store.db.execute(
                    'SELECT * FROM executions').fetchone()
                self.assertEqual('feature/repair-pr-88', execution['branch'])
                self.assertEqual(self.head_sha, self.git(
                    execution['worktree'], 'rev-parse', 'HEAD'))
                self.assertIn('非 force 推回原 PR 分支 feature/mocx/pr88',
                              (self.cfg_dir / 'state' / (
                                  'trigger-prompt-%s.txt' % execution['id'])
                               ).read_text(encoding='utf-8'))
            finally:
                service.store.db.close()
        finally:
            self.git(self.repo, 'worktree', 'remove', str(foreign))

    def test_unclaimable_repair_target_is_acked_without_a_worktree(self):
        service = self.coordinator.service_for('a__b')
        try:
            service.set_meta('synced_at', time.time())
            service.set_meta('current_versions', {})
            row, _ = service.enqueue_command('repair_round', 88)
            service.store.mark_dispatched(row['id'], 'agent-box')
        finally:
            service.store.db.close()
        self.agent.execute({'id': row['id'], 'action': 'repair_round',
                            'number': 88, 'project': 'a__b'})
        service = self.coordinator.service_for('a__b')
        try:
            latest = service.latest_command('repair_round', 88)
            self.assertEqual('FAILED', latest['state'])
            self.assertIn('CLAIM_UNAVAILABLE', latest['fail_reason'])
        finally:
            service.store.db.close()
        self.assertEqual([], self.spawned)
        self.assertFalse((self.worktrees / 'repair-pr-88').exists())


if __name__ == '__main__':
    test_cp_agent.unittest.main(verbosity=2)
