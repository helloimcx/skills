"""Remote worker half of the Control Plane.

Same public surface as ``cp.service.Service``: coordination state transitions
travel to the CP server over HTTP, while every git/subprocess fact (worktree
validation, verification runs, branch push) executes on this machine. The
machine-global config only points at the CP server; the server stays the
single policy authority — each claim returns the current policy subset, cached
under the project state so later CLI processes (bind/verify/publish) run local
checks with the same rules.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from .client import Client
from .runtime import git, resolve_verification, run_checks, run_gate_commands, validate_worktree

# Policy keys the worker applies locally; everything else stays server-side.
_APPLICABLE_POLICY = ('base', 'branch_prefixes', 'verification', 'verification_fast',
                      'required_checks', 'lease_seconds')


class WorkerService:
    def __init__(self, config, client=None):
        self.cfg = config
        self.server = config['server']
        self.client = client or Client(self.server)
        self._cache_path = Path(config['state_dir']) / 'policy.json'
        self._apply_policy(self._load_policy_cache())

    # -- policy cache -------------------------------------------------------
    def _load_policy_cache(self):
        try:
            cached = json.loads(self._cache_path.read_text(encoding='utf-8'))
            return cached if isinstance(cached, dict) else {}
        except (OSError, ValueError):
            return {}

    def _apply_policy(self, policy):
        for key in _APPLICABLE_POLICY:
            if key in policy:
                self.cfg[key] = policy[key]

    def _save_policy_cache(self, policy):
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(policy, ensure_ascii=False, sort_keys=True),
                                    encoding='utf-8')

    def _freeze_policy(self, execution, policy):
        """Pin the claim's policy beside its bind context.

        The shared cache above is refreshed by every claim on this machine, so
        a second round would otherwise swap the exam under an in-flight one.
        The freeze keeps each execution on the policy it was claimed under;
        the server's version hash still guards the outcome (a superseded round
        is cancelled as STALE_INPUT), so the freeze removes wasted re-runs
        rather than carrying correctness.
        """
        eid = (execution or {}).get('id')
        if not eid:
            return
        directory = Path(self.cfg['state_dir']) / 'executions'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (eid + '.policy.json')).write_text(
            json.dumps(policy, ensure_ascii=False, sort_keys=True), encoding='utf-8')

    def _read_frozen_policy(self, eid):
        path = Path(self.cfg['state_dir']) / 'executions' / (eid + '.policy.json')
        try:
            frozen = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return frozen if isinstance(frozen, dict) else {}

    def _frozen_cfg(self, eid):
        """Config for this execution's verification: the policy it was claimed
        under, falling back to the shared cache when no freeze exists (rounds
        claimed before this machine had the file, or a hand-run verify)."""
        frozen = self._read_frozen_policy(eid)
        if not frozen:
            return self.cfg
        return dict(self.cfg, **{k: frozen[k] for k in _APPLICABLE_POLICY if k in frozen})

    # -- coordination (server-side) ----------------------------------------
    def sync(self):
        return self.client.call('POST', self.client.endpoint('sync'), {}) or {}

    def claim(self, loop, worker=None, item_id=None, ttl=None):
        return self.claim_raw(loop, worker, item_id, ttl=ttl).get('execution')

    def claim_raw(self, loop, worker=None, item_id=None, ttl=None):
        # The server composes <machine>/<suffix> (cp/server.py), so an omitted
        # (or blank) worker contributes only the loop — never a second host
        # prefix. The full result carries the claimed item row (the round
        # preparer reads PR facts from it) and the current policy subset.
        worker = (worker or '').strip() or loop
        body = {'loop': loop, 'worker': worker, 'item': item_id}
        if ttl is not None:
            body['ttl'] = ttl
        result = self.client.call('POST', self.client.endpoint('claim'), body)
        if result.get('policy'):
            self._apply_policy(result['policy'])
            self._save_policy_cache(result['policy'])
        if result.get('policy'):
            self._freeze_policy(result.get('execution'), result['policy'])
        return result

    def status(self):
        return self.client.call('GET', self.client.endpoint('status'))

    def items(self, kind=None):
        query = '?kind=' + kind if kind else ''
        return self.client.call('GET', self.client.endpoint('list') + query)['items']

    def executions(self, loop=None):
        query = '?loop=' + loop if loop else ''
        result = self.client.call('GET', self.client.endpoint('executions') + query)
        return result.get('executions', [])

    def recent_executions(self, loop=None, limit=20):
        query = '?include_terminal=1&limit=%d' % limit
        if loop:
            query += '&loop=' + loop
        result = self.client.call('GET', self.client.endpoint('executions') + query)
        return result.get('executions', [])

    def execution_status(self, execution):
        query = urlencode({'execution_id': execution})
        result = self.client.call(
            'GET', self.client.endpoint('executions') + '?' + query)
        return result.get('execution')

    def abandon(self, execution, token):
        return self.client.call('POST', self.client.endpoint('abandon'),
                                {'execution': execution, 'token': token})

    # -- local execution + remote bookkeeping --------------------------------
    def bind(self, eid, token, worktree, branch=None):
        root = self.cfg.get('repo_root')
        if branch is None and root and Path(worktree).resolve() == Path(root).resolve():
            # GATE_REPAIR: the repair effect lives in the project checkout,
            # not a worktree — attest the checkout instead of isolating it.
            context = {'worktree': str(Path(worktree).resolve()), 'branch': None,
                       'head': git(worktree, 'rev-parse', 'HEAD')}
        else:
            context = validate_worktree(self.cfg, worktree, branch)
        result = self.client.call('POST', self.client.endpoint('bind'),
                                  {'execution': eid, 'token': token, 'context': context})
        # The server's bind reply carries the task the context serves (gate
        # commands for GATE_REPAIR) and the item kind (verification tier
        # resolution); record them so a later verify process in a
        # separate CLI invocation can execute the right runner.
        stored = context
        if isinstance(result, dict):
            stored = dict(context, **{k: result[k] for k in
                                      ('task', 'gate_commands', 'kind') if k in result})
        self._record_bind_context(eid, stored)
        return result

    def verify(self, eid, token):
        # The bound worktree path was recorded at bind time on the server;
        # validate_worktree re-derives the same facts locally.
        e = self.execution(eid, token)
        if not e.get('worktree'):
            raise ValueError('BOUND_WORKTREE_REQUIRED')
        evidence_dir = Path(self.cfg['state_dir']) / 'executions' / eid / 'local'
        if e.get('task') == 'GATE_REPAIR':
            context = {'worktree': e['worktree'], 'branch': e.get('branch'), 'head': e.get('head')}
            checks = run_gate_commands(self.cfg, e.get('gate_commands') or [], e['worktree'],
                                       evidence_dir)
        else:
            context = validate_worktree(self.cfg, e['worktree'], e.get('branch'))
            frozen = self._frozen_cfg(eid)
            cfg = dict(frozen, verification=resolve_verification(frozen, e.get('kind')))
            checks = run_checks(cfg, context, evidence_dir)
        return self.client.call('POST', self.client.endpoint('verify'),
                                {'execution': eid, 'token': token,
                                 'context': context, 'checks': checks})

    def report(self, eid, token, result):
        return self.client.call('POST', self.client.endpoint('report'),
                                {'execution': eid, 'token': token, 'result': result})

    def execution(self, eid, token):
        """The bound worktree facts, from the bind context this machine wrote."""
        return self._bind_context(eid)

    def _bind_context(self, eid):
        path = Path(self.cfg['state_dir']) / 'executions' / (eid + '.json')
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def publish(self, eid, token):
        context = None
        bound = self._bind_context(eid)
        if bound.get('worktree'):
            # Re-validate locally right before the gate so the server-side
            # evidence comparisons use fresh worktree facts.
            context = validate_worktree(self.cfg, bound['worktree'], bound.get('branch'))
        prepared = self.client.call('POST', self.client.endpoint('publish/begin'),
                                    {'execution': eid, 'token': token, 'context': context})
        if prepared.get('batch_count') is not None:
            count = prepared['batch_count']
            receipts = []
            for index in range(count):
                current = prepared if index == 0 else self.client.call(
                    'POST', self.client.endpoint('publish/begin'),
                    {'execution': eid, 'token': token, 'context': context,
                     'action_index': index})
                if current.get('receipt') is not None:
                    receipts.append(current['receipt'])
                    continue
                if current.get('push_required'):
                    raise ValueError('BATCH_PUSH_NOT_SUPPORTED')
                receipts.append(self.client.call(
                    'POST', self.client.endpoint('publish/commit'),
                    {'execution': eid, 'token': token, 'op': current['op']}))
            return {'issues': receipts, 'execution_state': 'SUCCEEDED'}
        if prepared.get('receipt') is not None:
            return prepared['receipt']
        if prepared.get('push_required'):
            try:
                self._push(prepared)
            except Exception:
                self.client.call('POST', self.client.endpoint('publish/commit'),
                                 {'execution': eid, 'token': token, 'op': prepared['op'],
                                  'pushed': False})
                raise ValueError('PUBLICATION_UNKNOWN: query receipt before any retry') from None
            return self.client.call('POST', self.client.endpoint('publish/commit'),
                                    {'execution': eid, 'token': token, 'op': prepared['op'],
                                     'pushed': True})
        # No push involved: the server performs the platform write itself.
        return self.client.call('POST', self.client.endpoint('publish/commit'),
                                {'execution': eid, 'token': token, 'op': prepared['op']})

    def _push(self, prepared):
        execution = prepared['execution']
        worktree = execution['worktree']
        context = validate_worktree(self.cfg, worktree, execution['branch'])
        if context['head'] != execution['head']:
            raise ValueError('PUSH_CONTEXT_CHANGED')
        # Conflict repair may run on a temp branch; the push always lands on
        # the original PR branch (target_branch resolved server-side in
        # prepare_publish). Explicit refspec; ordinary non-forced push rejects
        # remote divergence.
        target = prepared.get('target_branch') or context['branch']
        git(worktree, 'push', 'origin', context['head'] + ':refs/heads/' + target)

    # -- deliberately server-local actions -----------------------------------
    def enable(self):
        raise ValueError('MACHINE_MANAGEMENT_REMOTE: enable/disable lives on the CP server '
                         '(cp_server.py machines ...)')

    def disable(self):
        raise ValueError('MACHINE_MANAGEMENT_REMOTE: enable/disable lives on the CP server '
                         '(cp_server.py machines ...)')

    def reconcile(self, operation, receipt):
        raise ValueError('OPERATOR_ACTION_ON_SERVER: run reconcile on the CP server machine')

    def _record_bind_context(self, eid, context):
        path = Path(self.cfg['state_dir']) / 'executions'
        path.mkdir(parents=True, exist_ok=True)
        (path / (eid + '.json')).write_text(json.dumps(context, ensure_ascii=False),
                                            encoding='utf-8')
