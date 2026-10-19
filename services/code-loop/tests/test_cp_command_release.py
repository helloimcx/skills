#!/usr/bin/env python3
"""Releasing a round when its spawn dies: the FAILED ack carries the claim
(execution + holder token) and the server hands the item back.

The command ledger and the execution are different owners of the same round —
the ledger records the instruction, the execution holds the item. A command
whose spawn died after the claim was taken used to leave the execution
RUNNING: a phantom "审核中" card and a held worktree for the whole lease. The
holder token is the same proof bind/report/publish already use, so no new
authority is invented. Split from test_cp_commands.py (800-line red line,
issue #185 convention: split, don't raise)."""
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp import server
from codeloop.cp.protocol import ProtocolError
from codeloop.cp.service import Service


class AckReleasesClaimTests(unittest.TestCase):
    """A FAILED ack that carries its claim (execution+token) hands the round
    back. A command whose spawn died after the claim was taken used to leave
    the execution RUNNING — a phantom '审核中' card and a held worktree for the
    whole lease — because the ledger is not the thing holding the item. The
    holder token is the same proof bind/report/publish use."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service({'state_dir': str(Path(self.tmp.name) / 'state'),
                                'platform': {'url': 'http://g', 'repo': 'a/b'},
                                'project_key': 'a__b', 'snapshot_ttl': 3600})
        self.machine = {'name': 'agent-box'}

    def tearDown(self):
        self.service.store.db.close()
        self.tmp.cleanup()

    def seed(self, number=88):
        """A claimed round plus its dispatched command: exactly what the agent
        holds when its spawn dies."""
        self.service.store.put_item('review:%d' % number, 'reviewer', 'v1',
                                    {'pr': {'number': number}},
                                    ['pr-%d' % number])
        execution = self.service.store.claim('reviewer', 'agent-box', ttl=14400,
                                             item_id='review:%d' % number)
        row, _ = self.service.enqueue_command('review_round', number)
        self.service.store.mark_dispatched(row['id'], 'agent-box')
        return row, execution

    def ack(self, row, **overrides):
        body = {'command': row['id'], 'outcome': 'FAILED'}
        body.update(overrides)
        return server._invoke_command_ack(self.service, body, self.machine)

    def scalar(self, query, params=()):
        return self.service.store.db.execute(query, params).fetchone()[0]

    def test_failed_ack_with_the_claim_releases_the_round(self):
        row, execution = self.seed()
        acked = self.ack(row, fail_reason='SPAWN_EXIT: rc=1',
                         execution=execution['id'], token=execution['token'])
        self.assertEqual('FAILED', acked['state'])
        self.assertEqual('CANCELLED', self.scalar(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)))
        self.assertEqual('READY', self.scalar(
            'SELECT state FROM work_items WHERE key=?', ('review:88',)))
        self.assertEqual(0, self.scalar(
            'SELECT COUNT(*) FROM leases WHERE execution_id=?', (execution['id'],)))

    def test_failed_ack_without_a_claim_changes_nothing(self):
        """Every pre-claim refusal (unregistered project, unknown item, bad
        model) acks without a claim and must behave exactly as before."""
        row, execution = self.seed()
        acked = self.ack(row, fail_reason='CLAIM_UNAVAILABLE: review:88 不存在')
        self.assertEqual('FAILED', acked['state'])
        self.assertEqual('RUNNING', self.scalar(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)))

    def test_wrong_token_never_releases_a_round_and_never_fails_the_ack(self):
        row, execution = self.seed()
        acked = self.ack(row, execution=execution['id'],
                         token=execution['token'] + 1)
        self.assertEqual('FAILED', acked['state'])     # the ack still landed
        self.assertEqual('RUNNING', self.scalar(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)))
        self.assertEqual(1, self.scalar(
            "SELECT COUNT(*) FROM events WHERE kind='command.abandon_failed'"))

    def test_spawned_ack_never_releases_the_round(self):
        row, execution = self.seed()
        acked = self.ack(row, outcome='SPAWNED', execution=execution['id'],
                         token=execution['token'])
        self.assertEqual('SPAWNED', acked['state'])
        self.assertEqual('RUNNING', self.scalar(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)))

    def test_a_lost_ack_never_releases_anything(self):
        """A duplicated or foreign ack is a domain rejection (the existing
        contract), and it must not release anything on its way out."""
        row, execution = self.seed()
        self.service.store.ack_command(row['id'], 'agent-box', 'FAILED')
        with self.assertRaises(ValueError) as caught:
            self.ack(row, execution=execution['id'], token=execution['token'])
        self.assertIn('COMMAND_STATE', str(caught.exception))
        self.assertEqual('RUNNING', self.scalar(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)))

    def test_malformed_release_fields_are_rejected(self):
        row, execution = self.seed()
        for token in ('3', 3.5, True):
            with self.assertRaises(ProtocolError) as caught:
                self.ack(row, execution=execution['id'], token=token)
            self.assertIn('token must be an integer', str(caught.exception))
        with self.assertRaises(ProtocolError):
            self.ack(row, execution=7, token=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
