"""Operator face: L1 verb whitelist/audit, report validation, fail_round; and
the round wiring: ingest→command enqueue, dispatch_operator (serve host only)."""
import json
import sys as _sys
import tempfile
import unittest
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.server import CommandDispatcher
from codeloop.operator import config as op_config
from codeloop.operator import face, ingest as op_ingest
from codeloop.operator import store as op_store


def cfg():
    return op_config.load_operator_config({'operator': {'enabled': True}})


class FaceRoundBase(unittest.TestCase):
    def setUp(self):
        # Serve-side setup wires additional process-global verbs. Keep these
        # unit tests independent of collection order and the server profile.
        self.saved_face_wired = set(face.WIRED)
        self.saved_face_executor = face._EXECUTOR
        face.WIRED.clear()
        face.WIRED.update({'no_op', 'sweep_expired'})
        face._EXECUTOR = None
        self.home = tempfile.mkdtemp(prefix='op-face-')
        self.store = op_store.open_store(self.home)
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={'message': 'stall'},
                                      org_repo='mocx__skills', now=1_000.0)
        self.claim = face.claim(self.store, worker='HOST/operator', ttl=1_800,
                                item_id=item['id'])

    def tearDown(self):
        self.store.db.close()
        face.WIRED.clear()
        face.WIRED.update(self.saved_face_wired)
        face._EXECUTOR = self.saved_face_executor


class ActTests(FaceRoundBase):
    def test_wired_verbs_execute_and_audit(self):
        receipt = face.act(self.store, self.claim['id'], self.claim['token'],
                           'no_op')
        self.assertTrue(receipt['ok'])
        events = face._act_events(self.store, self.claim['id'])
        self.assertEqual([e['verb'] for e in events], ['no_op'])

    def test_sweep_expired_counts(self):
        receipt = face.act(self.store, self.claim['id'], self.claim['token'],
                           'sweep_expired')
        self.assertTrue(receipt['ok'])
        self.assertIn('swept', receipt)

    def test_unknown_and_unwired_verbs_refused(self):
        with self.assertRaises(ValueError) as bad:
            face.act(self.store, self.claim['id'], self.claim['token'], 'merge')
        self.assertIn('OPERATOR_VERB_INVALID', str(bad.exception))
        with self.assertRaises(ValueError) as unwired:
            face.act(self.store, self.claim['id'], self.claim['token'],
                     'collect_refresh')
        self.assertIn('OPERATOR_VERB_NOT_WIRED', str(unwired.exception))

    def test_fencing_required(self):
        with self.assertRaises(ValueError):
            face.act(self.store, self.claim['id'], 999, 'no_op')


class ReportTests(FaceRoundBase):
    def report(self, **over):
        result = dict({'signal': 'op:alert:k1', 'diagnosis': 'd', 'actions': [],
                       'outcome': 'RESOLVED', 'evidence': []}, **over)
        face.report(self.store, self.claim['id'], self.claim['token'], result)

    def test_valid_report_finalizes(self):
        self.report()
        row = self.store.db.execute('SELECT * FROM executions WHERE id=?',
                                    (self.claim['id'],)).fetchone()
        self.assertEqual(row['state'], 'SUCCEEDED')
        self.assertEqual(json.loads(row['result'])['outcome'], 'RESOLVED')

    def test_bad_outcome_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.report(outcome='MERGED')
        self.assertIn('OPERATOR_REPORT_INVALID', str(caught.exception))

    def test_unaudited_action_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.report(actions=[{'verb': 'sweep_expired', 'target': 'x'}])
        self.assertIn('no operator.act audit', str(caught.exception))
        # after a real act the same action passes
        face.act(self.store, self.claim['id'], self.claim['token'],
                 'sweep_expired')
        self.report(actions=[{'verb': 'sweep_expired', 'target': 'x'}])

    def test_wrong_signal_identity_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.report(signal='op:alert:other')
        self.assertIn('OPERATOR_REPORT_INVALID', str(caught.exception))

    def test_merge_verb_never_passes(self):
        with self.assertRaises(ValueError):
            self.report(actions=[{'verb': 'merge', 'target': 'pr-1'}])
        row = self.store.db.execute('SELECT state FROM executions WHERE id=?',
                                    (self.claim['id'],)).fetchone()
        self.assertEqual(row['state'], 'RUNNING')

    def test_fail_round_is_terminal_and_distinct(self):
        face.fail_round(self.store, self.claim['id'], self.claim['token'],
                        'PROCESS_DIED: exit=1')
        row = self.store.db.execute('SELECT * FROM executions WHERE id=?',
                                    (self.claim['id'],)).fetchone()
        self.assertEqual(row['state'], 'FAILED')
        self.assertIsNone(json.loads(row['result'])['outcome'])

    def test_fail_round_never_overwrites_terminal_report(self):
        """BLOCKER-1: the watcher outliving a session that already self-reported
        must not flip its SUCCEEDED verdict into FAILED — fail_round is a
        no-op on any terminal state."""
        result = {'outcome': 'RESOLVED', 'actions': [], 'signal': 'op:alert:k1'}
        face.report(self.store, self.claim['id'], self.claim['token'], result)
        row = self.store.db.execute('SELECT * FROM executions WHERE id=?',
                                    (self.claim['id'],)).fetchone()
        self.assertEqual(row['state'], 'SUCCEEDED')
        face.fail_round(self.store, self.claim['id'], self.claim['token'],
                        'PROCESS_DIED: exit=0')
        row = self.store.db.execute('SELECT * FROM executions WHERE id=?',
                                    (self.claim['id'],)).fetchone()
        self.assertEqual(row['state'], 'SUCCEEDED')
        self.assertEqual(json.loads(row['result'])['outcome'], 'RESOLVED')

    def test_ack_failed_binds_failure_to_execution_id(self):
        """MAJOR-2: the FAILED ack must read the execution id from the claim row
        (`claim['id']`, which store.claim returns), not a nonexistent
        `claim['execution']` key that turned every failure path into a second
        KeyError masking the original fault."""
        row, created = self.store.enqueue_command('operator_round', 1)
        self.assertTrue(created)
        self.assertIsNotNone(self.store.mark_dispatched(row['id'], 'HOST/operator'))
        self.assertIsNotNone(self.store.ack_command(
            row['id'], 'HOST/operator', 'SPAWNED', execution=self.claim['id']))
        face.ack_failed(self.store, row['id'], 'HOST/operator',
                        'OSError: boom', claim=self.claim)
        acked = self.store.db.execute('SELECT * FROM commands WHERE id=?',
                                      (row['id'],)).fetchone()
        self.assertEqual(acked['state'], 'FAILED')
        self.assertEqual(acked['fail_reason'], 'OSError: boom')
        self.assertEqual(acked['execution'], self.claim['id'])

    def test_ack_failed_without_claim_acks_unbound(self):
        """MAJOR-2: when there is no claim (spawn failed before claim), the
        FAILED ack itself must not raise."""
        row, created = self.store.enqueue_command('operator_round', 2)
        self.assertTrue(created)
        self.assertIsNotNone(self.store.mark_dispatched(row['id'], 'HOST/operator'))
        self.assertIsNone(face.ack_failed(self.store, row['id'], 'HOST/operator',
                                          'CLAIM_UNAVAILABLE', claim=None))


class IngestCommandTests(unittest.TestCase):
    def test_fired_signal_enqueues_operator_round_command(self):
        home = tempfile.mkdtemp(prefix='op-cmd-')
        store = op_store.open_store(home)
        try:
            alert = {'severity': 'high', 'rule': 'loop_stall', 'org_repo': 'm',
                     'key': 'k1', 'message': 'stall'}
            op_ingest.ingest_alerts(store, cfg(), [alert], now=1_000.0, home=home)
            pending = store.pending_commands()
            self.assertEqual([c['action'] for c in pending], ['operator_round'])
            self.assertIsNotNone(op_store.item_for_number(store, pending[0]['number']))
        finally:
            store.db.close()


class FakeConn:
    def __init__(self):
        self.frames = []

    def send_json(self, payload):
        self.frames.append(payload)
        return None


class DispatchOperatorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-disp-')
        self.store = op_store.open_store(self.home)
        self.dispatcher = CommandDispatcher(coordinator=None)

    def tearDown(self):
        self.store.db.close()

    def bind(self, name):
        conn = FakeConn()
        with self.dispatcher._lock:
            self.dispatcher._connections['m1'] = conn
            self.dispatcher._machines['m1'] = {'name': name}
        return conn

    def test_dispatches_to_serve_host_only(self):
        import socket
        self.bind('OTHER-MACHINE')
        alert = {'severity': 'high', 'rule': 'loop_stall', 'org_repo': 'm',
                 'key': 'k1', 'message': 'stall'}
        op_ingest.ingest_alerts(self.store, cfg(), [alert], now=1_000.0,
                                home=self.home)
        self.assertEqual(self.dispatcher.dispatch_operator(store=self.store), 0)
        conn = self.bind(socket.gethostname())
        self.assertEqual(self.dispatcher.dispatch_operator(store=self.store), 1)
        frame = conn.frames[0]['command']
        self.assertEqual(frame['action'], 'operator_round')
        self.assertEqual(frame['project'], 'operator')
        self.assertTrue(frame.get('item'))
        # the command left the ledger (claimed → spawned-by-send) — a second
        # pass sends nothing new
        self.assertEqual(self.dispatcher.dispatch_operator(store=self.store), 0)

    def test_no_connection_leaves_command_pending(self):
        alert = {'severity': 'high', 'rule': 'loop_stall', 'org_repo': 'm',
                 'key': 'k1', 'message': 'stall'}
        op_ingest.ingest_alerts(self.store, cfg(), [alert], now=1_000.0,
                                home=self.home)
        self.assertEqual(self.dispatcher.dispatch_operator(store=self.store), 0)
        self.assertEqual(len(self.store.pending_commands()), 1)

    def test_spawn_options_ride_along_when_set(self):
        """Operator cfg/dialog agent+model must survive into the dispatched
        frame — without the conditional carriage the round always falls back
        to DEFAULT_COMMAND (review MAJOR)."""
        import socket
        conn = self.bind(socket.gethostname())
        self.store.enqueue_command('operator_round', 7, agent='opencode',
                                   model='kimi-for-coding/k3')
        self.assertEqual(self.dispatcher.dispatch_operator(store=self.store), 1)
        frame = conn.frames[0]['command']
        self.assertEqual(frame['action'], 'operator_round')
        self.assertEqual(frame['agent'], 'opencode')
        self.assertEqual(frame['model'], 'kimi-for-coding/k3')
        # and when unset the frame stays a plain operator_round
        conn2 = self.bind(socket.gethostname())
        self.store.enqueue_command('operator_round', 8)
        self.assertEqual(self.dispatcher.dispatch_operator(store=self.store), 1)
        frame2 = conn2.frames[0]['command']
        self.assertNotIn('agent', frame2)
        self.assertNotIn('model', frame2)


if __name__ == '__main__':
    unittest.main()
