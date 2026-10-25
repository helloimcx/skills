#!/usr/bin/env python3
"""review_round: the shared claim -> worktree -> bind chain.

prepare_round is exercised against a scripted coordinator (the interface
contract: claim, pr_facts, bind, abandon_quiet), and the real carrier —
RemoteCoordinator over a WorkerService — against a scripted transport. The
agent end-to-end (real store, real repo) lives in test_cp_agent.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop import review_round
from codeloop.cp.client import ClientError
from codeloop.cp.worker import WorkerService


class FakeGit:
    """Scripted git: ``fail_on`` matches the first two argv tokens."""

    def __init__(self, head='', object_present=True, fail_on=(),
                 worktrees='', local_branch_tip=None, local_branch_missing=False):
        self.head = head
        self.object_present = object_present
        self.fail_on = tuple(fail_on)
        self.worktrees = worktrees
        self.local_branch_tip = local_branch_tip
        self.local_branch_missing = local_branch_missing
        self.calls = []

    def __call__(self, root, *args):
        self.calls.append((str(root),) + tuple(str(a) for a in args))
        key = ' '.join(str(a) for a in args[:2])
        if key in self.fail_on or (args and args[0] in self.fail_on):
            raise ValueError('GIT_FAILED: scripted failure in %s' % key)
        return self._answer(args)

    def _answer(self, args):
        if args and args[0] == 'worktree' and args[1] == 'list':
            return self.worktrees
        if args and args[0] == 'cat-file':
            return self._cat_file()
        if args and args[0] == 'rev-parse':
            return self._rev_parse(args)
        return ''

    def _cat_file(self):
        if not self.object_present:
            raise ValueError('GIT_FAILED: missing object')
        return self.head

    def _rev_parse(self, args):
        if args[1:3] != ('--verify', '--quiet'):
            return self.head
        if self.local_branch_missing:
            raise ValueError('GIT_FAILED: no such ref')
        return self.head if self.local_branch_tip is None else self.local_branch_tip


class ScriptedCoordinator:
    """The coordinator interface as a recording double — prepare_round's
    contract is what matters here, not which carrier implements it."""

    def __init__(self, claim=None, pr=None, claim_error=None, bind_error=None,
                 loop='reviewer'):
        self.claim_result = claim
        self.pr = pr
        self.claim_error = claim_error
        self.bind_error = bind_error
        self.loop = loop
        self.claims = []
        self.binds = []
        self.abandoned = []

    def claim(self, number, ttl=review_round.PRECLAIM_TTL_SECONDS):
        self.claims.append((number, ttl))
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_result

    def pr_facts(self, claim):
        if self.pr is None:
            raise ValueError('CLAIMED_ITEM_MISSING: %s' % claim.get('id'))
        return self.pr

    def bind(self, claim, worktree, branch=None):
        self.binds.append((claim['id'], claim['token'], worktree, branch))
        if self.bind_error is not None:
            raise self.bind_error

    def abandon_quiet(self, execution, token):
        self.abandoned.append((execution, token))


class FakeClient:
    """Scripts ``client.call`` the way codeloop.cp.client raises: domain
    rejections arrive as ValueError with the server's message, transport
    failures as CP_SERVER_* ValueErrors, TLS-level as ClientError."""

    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    def endpoint(self, verb):
        return '/api/projects/a__b/%s' % verb

    def call(self, method, path, body=None, token=None, raw_body=None):
        self.calls.append((method, path, body))
        if self.error is not None:
            raise self.error
        return self.result


def make_worker(tmp, client):
    state = Path(tmp) / 'state'
    state.mkdir(parents=True, exist_ok=True)
    return WorkerService({'server': {'url': 'http://cp.example'},
                          'state_dir': str(state),
                          'repo_root': str(Path(tmp) / 'repo'),
                          'worktree_root': str(Path(tmp) / 'wt'),
                          'base': 'main', 'branch_prefixes': ['feature/']},
                         client=client)


def pr_payload(number=88, sha='head88'):
    return json.dumps({'pr': {'number': number, 'title': 'PR %d' % number,
                              'head': {'ref': 'feature/x', 'sha': sha}}})


class PrepareRoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cp_cfg = {'repo_root': str(Path(self.tmp.name) / 'repo'),
                       'worktree_root': str(Path(self.tmp.name) / 'wt')}
        self.worktree = str(Path(self.tmp.name) / 'wt' / 'review-pr-88')
        self.git = FakeGit(head='head88')

    def tearDown(self):
        self.tmp.cleanup()

    def coordinator(self, **kw):
        kw.setdefault('claim', {'id': 'exec-1', 'token': 3, 'item_id': 'item-1'})
        kw.setdefault('pr', {'number': 88, 'head': {'ref': 'feature/x',
                                                    'sha': 'head88'}})
        return ScriptedCoordinator(**kw)

    def test_happy_path_claims_reads_pr_cuts_worktree_binds(self):
        coordinator = self.coordinator()
        round_claim = review_round.prepare_round(coordinator, self.cp_cfg, 88,
                                                git_fn=self.git)
        self.assertEqual({'execution': 'exec-1', 'token': 3,
                          'worktree': self.worktree, 'branch': None,
                          'head_ref': None, 'head_sha': 'head88',
                          'base_ref': None, 'base_sha': None}, round_claim)
        # the short handoff lease is what makes an adopted claim self-heal
        self.assertEqual([(88, 300)], coordinator.claims)
        self.assertEqual([('exec-1', 3, self.worktree, None)], coordinator.binds)
        self.assertEqual([], coordinator.abandoned)
        self.assertEqual([(self.cp_cfg['repo_root'], 'cat-file', '-e',
                           'head88^{commit}'),
                          (self.cp_cfg['repo_root'], 'worktree', 'add', '--detach',
                           self.worktree, 'head88'),
                          (self.worktree, 'rev-parse', 'HEAD')], self.git.calls)

    def test_unclaimable_never_touches_git(self):
        coordinator = self.coordinator(claim_error=review_round.PrepareError(
            409, 'CLAIM_UNAVAILABLE: review:88 当前不可领取'))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=self.git)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual([], self.git.calls)
        self.assertEqual([], coordinator.abandoned)

    def test_missing_item_row_hands_the_claim_back(self):
        coordinator = self.coordinator(pr=None)
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=self.git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('CLAIM_FAILED', caught.exception.message)
        self.assertEqual([('exec-1', 3)], coordinator.abandoned)

    def test_worktree_failure_hands_the_claim_back(self):
        coordinator = self.coordinator()
        git = FakeGit(head='head88', fail_on=('worktree add',))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('WORKTREE_FAILED', caught.exception.message)
        self.assertEqual([('exec-1', 3)], coordinator.abandoned)

    def test_head_mismatch_refuses_and_cleans_up(self):
        coordinator = self.coordinator(pr={'number': 88, 'head': {
            'ref': 'feature/x', 'sha': 'other'}})
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=self.git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('HEAD head88 != PR head other', caught.exception.message)
        self.assertIn((self.cp_cfg['repo_root'], 'worktree', 'remove', self.worktree),
                      self.git.calls)
        self.assertEqual([('exec-1', 3)], coordinator.abandoned)

    def test_bind_failure_hands_the_claim_back_and_removes_the_worktree(self):
        coordinator = self.coordinator(bind_error=ValueError('PR_HEAD_MISMATCH: x'))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=self.git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('BIND_FAILED', caught.exception.message)
        self.assertIn((self.cp_cfg['repo_root'], 'worktree', 'remove', self.worktree),
                      self.git.calls)


class RepairRoundTests(unittest.TestCase):
    """prepare_round on the handler loop: claim repair:<n>, cut a *branch*
    worktree the session can push to — the original PR branch when it is free
    and exactly at the platform head, else a temp repair branch starting at
    the head SHA (the two shapes the CP bind guard accepts for
    PR_CONFLICT_RESOLUTION)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cp_cfg = {'repo_root': str(Path(self.tmp.name) / 'repo'),
                       'worktree_root': str(Path(self.tmp.name) / 'wt'),
                       'branch_prefixes': ['feature/mocx/', 'fix/mocx/']}
        self.worktree = str(Path(self.tmp.name) / 'wt' / 'repair-pr-88')
        self.git = FakeGit(head='head88')

    def tearDown(self):
        self.tmp.cleanup()

    def coordinator(self, **kw):
        kw.setdefault('loop', 'handler')
        kw.setdefault('claim', {'id': 'exec-1', 'token': 3, 'item_id': 'item-1'})
        kw.setdefault('pr', {'number': 88, 'head': {'ref': 'feature/mocx/x',
                                                    'sha': 'head88'}})
        return ScriptedCoordinator(**kw)

    def test_free_branch_at_head_is_checked_out(self):
        coordinator = self.coordinator()
        round_claim = review_round.prepare_round(coordinator, self.cp_cfg, 88,
                                                 git_fn=self.git)
        self.assertEqual('feature/mocx/x', round_claim['branch'])
        self.assertEqual([('exec-1', 3, self.worktree, 'feature/mocx/x')],
                         coordinator.binds)
        self.assertIn((self.cp_cfg['repo_root'], 'worktree', 'add',
                       self.worktree, 'feature/mocx/x'), self.git.calls)

    def test_occupied_branch_falls_back_to_temp_branch(self):
        """git allows one worktree per branch: a foreign worktree holding the
        PR branch forces the temp repair branch, start pinned to the head."""
        git = FakeGit(head='head88',
                      worktrees='worktree /elsewhere\nbranch refs/heads/feature/mocx/x\n')
        coordinator = self.coordinator()
        round_claim = review_round.prepare_round(coordinator, self.cp_cfg, 88,
                                                 git_fn=git)
        self.assertEqual('fix/mocx/repair-pr-88', round_claim['branch'])
        self.assertIn((self.cp_cfg['repo_root'], 'worktree', 'add', '-B',
                       'fix/mocx/repair-pr-88', self.worktree, 'head88'),
                      git.calls)

    def test_stale_local_branch_falls_back_to_temp_branch(self):
        """A local branch behind (or ahead of) the platform head must not be
        moved silently: the temp branch starts at the reviewed head instead —
        merging anything less would silently drop PR commits."""
        git = FakeGit(head='head88', local_branch_tip='old88')
        coordinator = self.coordinator()
        round_claim = review_round.prepare_round(coordinator, self.cp_cfg, 88,
                                                 git_fn=git)
        self.assertEqual('fix/mocx/repair-pr-88', round_claim['branch'])

    def test_missing_local_branch_falls_back_to_temp_branch(self):
        git = FakeGit(head='head88', local_branch_missing=True)
        coordinator = self.coordinator()
        round_claim = review_round.prepare_round(coordinator, self.cp_cfg, 88,
                                                 git_fn=git)
        self.assertEqual('fix/mocx/repair-pr-88', round_claim['branch'])

    def test_branch_outside_prefix_convention_falls_back_to_temp(self):
        """A PR head branch the project's prefix list does not allow cannot be
        checked out for a round at all (validate_worktree would refuse the
        bind) — go straight to the temp branch instead of failing the same
        bind on every retry."""
        cp_cfg = dict(self.cp_cfg, branch_prefixes=['feature/mocx/'])
        git = FakeGit(head='head88')   # local tip == head, branch free
        coordinator = self.coordinator(
            pr={'number': 88, 'head': {'ref': 'patch-1', 'sha': 'head88'}})
        round_claim = review_round.prepare_round(coordinator, cp_cfg, 88,
                                                 git_fn=git)
        self.assertEqual('feature/mocx/repair-pr-88', round_claim['branch'])

    def test_temp_branch_prefix_follows_project_config(self):
        cp_cfg = dict(self.cp_cfg, branch_prefixes=['feature/'])
        git = FakeGit(head='head88', local_branch_missing=True)
        round_claim = review_round.prepare_round(self.coordinator(), cp_cfg, 88,
                                                 git_fn=git)
        self.assertEqual('feature/repair-pr-88', round_claim['branch'])

    def test_head_mismatch_refuses_and_cleans_up(self):
        git = FakeGit(head='other')
        coordinator = self.coordinator()
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('HEAD other != PR head head88', caught.exception.message)
        self.assertIn((self.cp_cfg['repo_root'], 'worktree', 'remove',
                       self.worktree), git.calls)
        self.assertEqual([('exec-1', 3)], coordinator.abandoned)

    def test_worktree_failure_hands_the_claim_back(self):
        git = FakeGit(head='head88', fail_on=('worktree add',))
        coordinator = self.coordinator()
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88, git_fn=git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('WORKTREE_FAILED', caught.exception.message)
        self.assertEqual([('exec-1', 3)], coordinator.abandoned)

    def test_bind_failure_hands_the_claim_back_and_removes_the_worktree(self):
        coordinator = self.coordinator(bind_error=ValueError('BRANCH_LOCKED'))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.prepare_round(coordinator, self.cp_cfg, 88,
                                       git_fn=self.git)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('BIND_FAILED', caught.exception.message)
        self.assertIn((self.cp_cfg['repo_root'], 'worktree', 'remove',
                       self.worktree), self.git.calls)


class RemoteCoordinatorLoopTests(unittest.TestCase):
    """The loop decides both the worker queue and the item-key prefix: a
    repair round claims repair:<n> on the handler queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claim_result = {
            'execution': {'id': 'exec-9', 'token': 7, 'item_id': 'item-9'},
            'item': {'id': 'item-9', 'key': 'repair:88', 'payload': pr_payload()},
            'policy': {}}

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, result=None):
        return make_worker(self.tmp.name, FakeClient(
            result=self.claim_result if result is None else result))

    def test_repair_loop_claims_the_repair_item(self):
        worker = self.worker()
        claim = review_round.RemoteCoordinator(worker, loop='handler').claim(88)
        self.assertEqual('exec-9', claim['id'])
        method, path, body = worker.client.calls[0]
        self.assertEqual(('handler', 'repair:88', 300),
                         (body['worker'], body['item'], body['ttl']))

    def test_bind_carries_the_branch(self):
        worker = self.worker()
        coordinator = review_round.RemoteCoordinator(worker, loop='handler')
        claim = coordinator.claim(88)
        sent = []
        worker.bind = lambda eid, token, wt, branch=None: sent.append(
            (eid, token, wt, branch))
        coordinator.bind(claim, 'C:/wt/x', branch='feature/mocx/x')
        self.assertEqual([('exec-9', 7, 'C:/wt/x', 'feature/mocx/x')], sent)

    def test_unknown_loop_is_a_loud_refusal(self):
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(self.worker(), loop='discover').claim(88)
        self.assertEqual(400, caught.exception.status)

    def test_blocked_claim_names_the_blocker_and_unlock(self):
        """issue #254: a directed claim that loses to a resource conflict
        reports who holds the resources and when they unlock, instead of the
        generic three-way guess."""
        blocked = {'execution': None, 'item': None, 'policy': {},
                   'blocked_by': [{'item_key': 'issue:250', 'kind': 'handler',
                                   'worker': 'm1/handler', 'state': 'RUNNING',
                                   'resources': ['path:*'],
                                   'expires_at': 1000.0 + 3600,
                                   'stealable': False}]}
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(self.worker(blocked), loop='handler').claim(88)
        self.assertEqual(409, caught.exception.status)
        self.assertIn('issue:250', caught.exception.message)
        self.assertIn('path:*', caught.exception.message)
        self.assertIn('租约至', caught.exception.message)
        self.assertNotIn('评审并发已满', caught.exception.message)

    def test_blocked_claim_without_detail_keeps_the_legacy_wording(self):
        blocked = {'execution': None, 'item': None, 'policy': {}}
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(self.worker(blocked), loop='handler').claim(88)
        self.assertEqual(409, caught.exception.status)
        self.assertIn('当前不可领取', caught.exception.message)


class RemoteCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.claim_result = {
            'execution': {'id': 'exec-9', 'token': 7, 'item_id': 'item-9'},
            'item': {'id': 'item-9', 'key': 'review:88', 'payload': pr_payload()},
            'policy': {}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_sends_bare_loop_item_and_ttl(self):
        client = FakeClient(result=self.claim_result)
        worker = make_worker(self.tmp.name, client)
        coordinator = review_round.RemoteCoordinator(worker)
        claim = coordinator.claim(88)
        self.assertEqual('exec-9', claim['id'])
        method, path, body = client.calls[0]
        self.assertEqual(('POST', '/api/projects/a__b/claim'), (method, path))
        self.assertEqual(('reviewer', 'review:88', 300),
                         (body['worker'], body['item'], body['ttl']))

    def test_pr_facts_come_from_the_claim_response(self):
        worker = make_worker(self.tmp.name, FakeClient(result=self.claim_result))
        coordinator = review_round.RemoteCoordinator(worker)
        claim = coordinator.claim(88)
        self.assertEqual(88, coordinator.pr_facts(claim)['number'])

    def test_empty_queue_is_409(self):
        worker = make_worker(self.tmp.name,
                             FakeClient(result={'execution': None, 'item': None}))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(worker).claim(88)
        self.assertEqual(409, caught.exception.status)
        self.assertIn('CLAIM_UNAVAILABLE', caught.exception.message)

    def test_domain_rejection_keeps_the_server_code(self):
        worker = make_worker(self.tmp.name, FakeClient(
            error=ValueError('ITEM_UNKNOWN: claim --item takes a full item id or key')))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(worker).claim(88)
        self.assertEqual(409, caught.exception.status)
        self.assertIn('CLAIM_UNAVAILABLE', caught.exception.message)

    def test_transport_failure_is_502_not_unavailable(self):
        worker = make_worker(self.tmp.name, FakeClient(
            error=ValueError('CP_SERVER_UNREACHABLE: connection refused')))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(worker).claim(88)
        self.assertEqual(502, caught.exception.status)
        self.assertIn('CLAIM_FAILED', caught.exception.message)
        self.assertIn('CP_SERVER_UNREACHABLE', caught.exception.message)

    def test_tls_level_failure_is_502(self):
        worker = make_worker(self.tmp.name,
                             FakeClient(error=ClientError('CP_SERVER_PIN_MISMATCH')))
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.RemoteCoordinator(worker).claim(88)
        self.assertEqual(502, caught.exception.status)

    def test_remote_has_no_abandon_surface(self):
        worker = make_worker(self.tmp.name, FakeClient(result=self.claim_result))
        coordinator = review_round.RemoteCoordinator(worker)
        coordinator.abandon_quiet('exec-9', 7)   # must be a silent no-op
        self.assertEqual([], worker.client.calls[1:])


class CpConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'repo').mkdir()
        self.row = {'config_path': None, 'repo_cwd': str(self.root / 'repo')}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_config_file_is_a_loud_503(self):
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.cp_config(self.row, str(self.root), 'a__b')
        self.assertEqual(503, caught.exception.status)
        self.assertIn('REGISTER_INVALID', caught.exception.message)

    def test_invalid_config_json_is_a_loud_503(self):
        cfg = self.root / 'a__b' / 'control-plane.json'
        cfg.parent.mkdir(parents=True)
        cfg.write_text('{not json', encoding='utf-8')
        with self.assertRaises(review_round.PrepareError) as caught:
            review_round.cp_config(self.row, str(self.root), 'a__b')
        self.assertEqual(503, caught.exception.status)
        self.assertIn('REGISTER_INVALID', caught.exception.message)


if __name__ == '__main__':
    unittest.main(verbosity=2)
