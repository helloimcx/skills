#!/usr/bin/env python3
"""Machine registry: one-time enrollment, hashed tokens, grants."""
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

from codeloop.cp.registry import Registry, RegistryError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'registry.sqlite')
        self.r = Registry(self.path)

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def test_enroll_code_lifecycle(self):
        code = self.r.create_enroll_code('dev-b', ['a__b'], ttl=600)
        out = self.r.enroll(code, agent_version='1.0', addr='10.0.0.8')
        self.assertEqual(out['name'], 'dev-b')
        self.assertTrue(out['token'])
        with self.assertRaisesRegex(RegistryError, 'ENROLL_CODE_USED'):
            self.r.enroll(code, agent_version='1.0', addr='10.0.0.8')
        with self.assertRaisesRegex(RegistryError, 'ENROLL_CODE_INVALID'):
            self.r.enroll('no-such-code', agent_version='1.0', addr='x')

    def test_enroll_code_expires(self):
        code = self.r.create_enroll_code('dev-c', ['a__b'], ttl=600)
        self.r.db.execute('UPDATE enroll_codes SET expires_at=?', (time.time() - 1,))
        self.r.db.commit()
        with self.assertRaisesRegex(RegistryError, 'ENROLL_CODE_EXPIRED'):
            self.r.enroll(code, agent_version='1.0', addr='x')

    def test_token_authenticates_and_revokes(self):
        code = self.r.create_enroll_code('dev-b', ['a__b'])
        out = self.r.enroll(code, agent_version='1.0', addr='10.0.0.8')
        row = self.r.authenticate(out['token'])
        self.assertIsNotNone(row)
        self.assertEqual(row['name'], 'dev-b')
        self.assertIsNone(self.r.authenticate('forged'))
        self.r.touch(row['id'], addr='10.0.0.9', agent_version='1.1')
        self.assertGreater(self.r.authenticate(out['token'])['last_seen'], 0)
        self.r.revoke('dev-b')
        self.assertEqual(self.r.authenticate(out['token'])['revoked'], 1)
        # Rotation issues a fresh token and kills the old one.
        new = self.r.rotate('dev-b')
        self.assertIsNone(self.r.authenticate(out['token']))
        self.assertIsNotNone(self.r.authenticate(new))

    def test_enable_disable_toggle(self):
        code = self.r.create_enroll_code('dev-b', ['a__b'])
        out = self.r.enroll(code, agent_version='1.0', addr='x')
        self.r.set_enabled('dev-b', False)
        self.assertEqual(self.r.authenticate(out['token'])['enabled'], 0)
        self.r.set_enabled('dev-b', True)
        self.assertEqual(self.r.authenticate(out['token'])['enabled'], 1)

    def test_grants(self):
        code = self.r.create_enroll_code('dev-b', ['a__b'])
        out = self.r.enroll(code, agent_version='1.0', addr='x')
        self.assertTrue(self.r.has_grant(out['machine_id'], 'a__b'))
        self.assertFalse(self.r.has_grant(out['machine_id'], 'other__c'))
        self.r.ungrant('dev-b', 'a__b')
        self.assertFalse(self.r.has_grant(out['machine_id'], 'a__b'))
        self.r.grant('dev-b', 'other__c')
        self.assertTrue(self.r.has_grant(out['machine_id'], 'other__c'))

    def test_duplicate_machine_name_rejected(self):
        self.r.create_enroll_code('dev-b', ['a__b'])
        with self.assertRaisesRegex(RegistryError, 'MACHINE_NAME_TAKEN'):
            self.r.create_enroll_code('dev-b', ['a__b'])

    def test_tokens_stored_hashed_only(self):
        code = self.r.create_enroll_code('dev-b', ['a__b'])
        out = self.r.enroll(code, agent_version='1.0', addr='x')
        raw = Path(self.path).read_bytes()
        self.assertNotIn(out['token'].encode(), raw)
        rows = self.r.db.execute('SELECT token_hash FROM machines').fetchall()
        self.assertTrue(all(len(r['token_hash']) == 64 for r in rows))


def _sha256(text):
    import hashlib
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class JoinTests(unittest.TestCase):
    """Worker-initiated pairing: join → approve → consume (token issuance).

    Trust anchors: the pairing secret never travels until pickup (hash only at
    join), and the server-side approve auto-compares the worker-reported cert
    fingerprint against this server's own certificate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'registry.sqlite')
        self.r = Registry(self.path)
        self.now = 1789000000.0
        self.secret = 'pairing-secret-0123456789abcdef'
        self.secret_hash = _sha256(self.secret)

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def join(self, name='gpu-box', fp='cafebabe', now=None):
        return self.r.create_join(name, self.secret_hash, fp, agent_version='1',
                                  now=self.now if now is None else now)

    def test_join_lifecycle_happy_path(self):
        self.join()
        row = self.r.list_joins(now=self.now)[0]
        self.assertEqual('PENDING', row['state'])
        self.assertEqual('gpu-box', row['name'])
        out = self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                                  now=self.now + 60)
        self.assertEqual('APPROVED', out['state'])
        out = self.r.consume_join('gpu-box', self.secret, now=self.now + 120)
        self.assertEqual('gpu-box', out['name'])
        self.assertTrue(out['token'])
        machine = self.r.authenticate(out['token'])
        self.assertIsNotNone(machine)
        self.assertTrue(self.r.has_grant(machine['id'], 'a__b'))
        self.assertFalse(self.r.has_grant(machine['id'], 'other__x'))
        self.assertEqual('CONSUMED',
                         self.r.db.execute('SELECT state FROM join_requests').fetchone()['state'])
        with self.assertRaisesRegex(RegistryError, 'JOIN_ALREADY_CONSUMED'):
            self.r.consume_join('gpu-box', self.secret, now=self.now + 130)

    def test_join_name_taken_by_existing_machine(self):
        code = self.r.create_enroll_code('gpu-box', ['a__b'])
        self.r.enroll(code, agent_version='1.0', addr='10.0.0.8')
        with self.assertRaisesRegex(RegistryError, 'JOIN_NAME_TAKEN'):
            self.join('gpu-box')

    def test_join_duplicate_pending_name(self):
        self.join('gpu-box')
        with self.assertRaisesRegex(RegistryError, 'JOIN_NAME_PENDING'):
            self.join('gpu-box')

    def test_join_pending_expires_before_approve(self):
        self.join()
        with self.assertRaisesRegex(RegistryError, 'JOIN_EXPIRED'):
            self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                                now=self.now + 901)

    def test_join_fingerprint_mismatch_blocks_approve(self):
        self.join(fp='cafebabe')
        with self.assertRaisesRegex(RegistryError, 'JOIN_FINGERPRINT_MISMATCH') as caught:
            self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='deadbeef',
                                now=self.now + 10)
        self.assertIn('cafebabe', str(caught.exception))
        self.assertIn('deadbeef', str(caught.exception))
        # 拒绝后请求仍是 PENDING，修正指纹可再批准
        out = self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                                  now=self.now + 20)
        self.assertEqual('APPROVED', out['state'])

    def test_join_server_without_cert_skips_fingerprint_compare(self):
        self.join(fp='whatever-the-worker-saw')
        out = self.r.approve_join('gpu-box', ['a__b'], server_cert_fp=None,
                                  now=self.now + 10)
        self.assertEqual('APPROVED', out['state'])

    def test_join_reject(self):
        self.join('gpu-box')
        self.r.reject_join('gpu-box', now=self.now + 10)
        with self.assertRaisesRegex(RegistryError, 'JOIN_REJECTED'):
            self.r.consume_join('gpu-box', self.secret, now=self.now + 20)

    def test_join_rejected_name_can_rejoin(self):
        """A REJECTED join is terminal history: the same name may re-join
        (issue #145 — previously JOIN_NAME_PENDING forever)."""
        self.join('gpu-box')
        self.r.reject_join('gpu-box', now=self.now + 10)
        out = self.join('gpu-box', now=self.now + 20)
        self.assertEqual('PENDING', out['state'])
        # The fresh request is approvable and consumable.
        out = self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                                  now=self.now + 30)
        self.assertEqual('APPROVED', out['state'])
        out = self.r.consume_join('gpu-box', self.secret, now=self.now + 40)
        self.assertEqual('gpu-box', out['name'])

    def test_join_expired_name_can_rejoin(self):
        """An EXPIRED request (approve timed out) also frees the name for a
        re-join (issue #145)."""
        self.join('gpu-box')
        self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                            now=self.now + 10)
        with self.assertRaisesRegex(RegistryError, 'JOIN_EXPIRED'):
            self.r.consume_join('gpu-box', self.secret, now=self.now + 10 + 901)
        out = self.join('gpu-box', now=self.now + 10 + 902)
        self.assertEqual('PENDING', out['state'])

    def test_join_approve_unknown_name(self):
        with self.assertRaisesRegex(RegistryError, 'JOIN_NOT_FOUND'):
            self.r.approve_join('ghost', ['a__b'], server_cert_fp=None, now=self.now)

    def test_join_approve_renames(self):
        self.join('gpu-box')
        out = self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                                  as_name='gpu-01', now=self.now + 10)
        self.assertEqual('gpu-01', out['approved_name'])
        out = self.r.consume_join('gpu-box', self.secret, now=self.now + 20)
        self.assertEqual('gpu-01', out['name'])

    def test_join_approved_claim_window_expires(self):
        self.join()
        self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                            now=self.now + 10)
        with self.assertRaisesRegex(RegistryError, 'JOIN_EXPIRED'):
            self.r.consume_join('gpu-box', self.secret, now=self.now + 10 + 901)

    def test_join_wrong_secret_does_not_consume(self):
        self.join()
        self.r.approve_join('gpu-box', ['a__b'], server_cert_fp='cafebabe',
                            now=self.now + 10)
        with self.assertRaisesRegex(RegistryError, 'JOIN_SECRET_MISMATCH'):
            self.r.consume_join('gpu-box', 'wrong-secret', now=self.now + 20)
        out = self.r.consume_join('gpu-box', self.secret, now=self.now + 30)
        self.assertTrue(out['token'])

    def test_join_list_hides_terminal_states(self):
        self.join('gpu-a')
        self.join('gpu-b', fp='ff')
        self.r.reject_join('gpu-a', now=self.now + 5)
        self.r.approve_join('gpu-b', ['a__b'], server_cert_fp='ff', now=self.now + 6)
        states = {row['name']: row['state'] for row in self.r.list_joins(now=self.now + 7)}
        self.assertEqual({'gpu-b': 'APPROVED'}, states)

    def test_join_approve_without_projects_registers_bare(self):
        """注册与授权分离：零项目批准 = 纯注册，机器存在但零 grants；
        授权随后独立追加，无需重新注册。"""
        self.join()
        out = self.r.approve_join('gpu-box', [], server_cert_fp='cafebabe',
                                  now=self.now + 60)
        self.assertEqual('APPROVED', out['state'])
        out = self.r.consume_join('gpu-box', self.secret, now=self.now + 120)
        machine = self.r.authenticate(out['token'])
        self.assertIsNotNone(machine)
        self.assertFalse(self.r.has_grant(machine['id'], 'a__b'))
        self.r.grant('gpu-box', 'a__b')
        self.assertTrue(self.r.has_grant(machine['id'], 'a__b'))

    def test_join_approve_rejects_non_list_projects(self):
        self.join()
        with self.assertRaisesRegex(RegistryError, 'INVALID_JOIN_PROJECTS'):
            self.r.approve_join('gpu-box', 'a__b', server_cert_fp=None, now=self.now + 10)

    def test_join_approve_multi_project_regression(self):
        """多项目批准行为与解耦前一致（AC 回归）。"""
        self.join()
        self.r.approve_join('gpu-box', ['a__b', 'c__d'], server_cert_fp='cafebabe',
                            now=self.now + 10)
        out = self.r.consume_join('gpu-box', self.secret, now=self.now + 20)
        self.assertEqual(['a__b', 'c__d'], out['projects'])
        machine_id = self.r.authenticate(out['token'])['id']
        self.assertTrue(self.r.has_grant(machine_id, 'a__b'))
        self.assertTrue(self.r.has_grant(machine_id, 'c__d'))


class GrantRequestTests(unittest.TestCase):
    """Worker-proposed project authorization: the worker can only propose
    (authenticated by its token); writing grants stays an operator decision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Registry(str(Path(self.tmp.name) / 'registry.sqlite'))
        code = self.r.create_enroll_code('gpu-box', [])
        self.machine_id = self.r.enroll(code, agent_version='1',
                                        addr='10.0.0.8')['machine_id']

    def tearDown(self):
        self.r.close()
        self.tmp.cleanup()

    def test_request_creates_pending_row(self):
        out = self.r.create_grant_request(self.machine_id, 'a__b')
        self.assertEqual('PENDING', out['state'])
        self.assertTrue(out['id'])
        rows = self.r.list_grant_requests()
        self.assertEqual(1, len(rows))
        self.assertEqual('gpu-box', rows[0]['machine_name'])
        self.assertEqual('a__b', rows[0]['org_repo'])

    def test_request_is_idempotent_while_pending(self):
        first = self.r.create_grant_request(self.machine_id, 'a__b')
        again = self.r.create_grant_request(self.machine_id, 'a__b')
        self.assertEqual(first['id'], again['id'])
        self.assertEqual(1, len(self.r.list_grant_requests()))

    def test_request_for_granted_project_short_circuits(self):
        self.r.grant('gpu-box', 'a__b')
        out = self.r.create_grant_request(self.machine_id, 'a__b')
        self.assertEqual('ALREADY_GRANTED', out['state'])
        self.assertEqual([], self.r.list_grant_requests())

    def test_request_unknown_machine(self):
        with self.assertRaisesRegex(RegistryError, 'MACHINE_NOT_FOUND'):
            self.r.create_grant_request('nope', 'a__b')

    def test_request_requires_project(self):
        with self.assertRaisesRegex(RegistryError, 'REQUEST_PROJECT_REQUIRED'):
            self.r.create_grant_request(self.machine_id, '')

    def test_approve_writes_grant(self):
        req = self.r.create_grant_request(self.machine_id, 'a__b')
        out = self.r.approve_grant_request(req['id'])
        self.assertEqual('APPROVED', out['state'])
        self.assertTrue(self.r.has_grant(self.machine_id, 'a__b'))
        with self.assertRaisesRegex(RegistryError, 'REQUEST_NOT_PENDING'):
            self.r.approve_grant_request(req['id'])

    def test_approve_unknown_request(self):
        with self.assertRaisesRegex(RegistryError, 'REQUEST_NOT_FOUND'):
            self.r.approve_grant_request('ghost')

    def test_reject_leaves_grants_untouched(self):
        req = self.r.create_grant_request(self.machine_id, 'a__b')
        self.r.reject_grant_request(req['id'])
        self.assertFalse(self.r.has_grant(self.machine_id, 'a__b'))
        with self.assertRaisesRegex(RegistryError, 'REQUEST_NOT_PENDING'):
            self.r.reject_grant_request(req['id'])


if __name__ == '__main__':
    unittest.main()
