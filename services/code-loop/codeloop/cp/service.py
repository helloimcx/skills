"""Deterministic reconciliation, evidence gates, and recoverable publication."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import re
import time

from . import evidence as evidence_mod
from . import gate as gate_mod
from . import blocked as blocked_mod
from .github import GitHub
from .commands import CommandLedgerMixin
from .gate import gate_key, linked_issues
from .report_coverage import coverage_line, validate_coverage
from .runtime import EXAM_PATH, default_worker, git, resolve_verification, run_checks, run_gate_commands, validate_worktree, version
from .store import Store
from . import baseline, exam, observer


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Service(CommandLedgerMixin):
    def __init__(self, config, platform=None, authorization=None):
        self.cfg = config
        self.store = Store(config['state_dir'])
        self._platform = platform
        self._platform_config = config['platform']
        # The CP server injects its registry decision here: machine
        # enablement was checked at the API layer, so no local marker applies.
        self._authorization = authorization
        # Repository exam in force for this project, read back from the last
        # sync (every request rebuilds the Service, so it must survive between
        # requests); empty means the machine config still supplies the exam.
        cached = self.meta('exam')
        self._repo_exam = exam.overlay(cached) if isinstance(cached, dict) else {}

    @property
    def platform(self):
        # Local status/list/claim operations must remain available when a
        # platform token is absent. Construct the adapter only at first sync or
        # publication, where the credential is actually needed.
        if self._platform is None:
            self._platform = GitHub(self._platform_config)
        return self._platform

    @platform.setter
    def platform(self, value):
        self._platform = value

    def _write_gates(self, mutate):
        """Transaction-scoped gate-ledger read-modify-write: server requests
        each get their own connection, so autocommit writes could interleave."""
        with self.store.transaction():
            gates = self.meta('gates') or {}
            mutate(gates)
            self.set_meta('gates', gates)

    def meta(self, key, default=None):
        row = self.store.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else default

    def set_meta(self, key, value):
        self.store.db.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (key, dump(value)))

    def snapshot(self):
        return self.meta('snapshot')

    def enabled(self):
        """Machine-local write permission: created by `enable`, removed by `disable`."""
        if self._authorization is not None:
            return self._authorization
        marker = Path(self.cfg['state_dir']) / 'machine-enabled.json'
        if not marker.is_file():
            raise ValueError('MACHINE_NOT_ENABLED: run `control_plane.py --config <config> enable` on this machine')
        try:
            return json.loads(marker.read_text(encoding='utf-8'))
        except ValueError:
            raise ValueError('MACHINE_NOT_ENABLED: corrupt marker; run disable then enable') from None

    def enable(self):
        # load_config already enforces the pinned certificate, so enabling implies
        # this machine satisfies the write prerequisites.
        marker = Path(self.cfg['state_dir']) / 'machine-enabled.json'
        info = {'host': platform.node(), 'enabled_at': time.time()}
        created = not marker.is_file()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(info), encoding='utf-8')
        self.store.event('machine.enabled' if created else 'machine.re_enabled', info)
        return dict(info, created=created, state_dir=self.cfg['state_dir'])

    def disable(self):
        marker = Path(self.cfg['state_dir']) / 'machine-enabled.json'
        existed = marker.is_file()
        if existed:
            marker.unlink()
        info = {'host': platform.node()}
        self.store.event('machine.disabled', info)
        return dict(info, removed=existed)

    def fresh(self):
        if self.meta('sync_error') or time.time() - self.meta('synced_at', 0) > self.cfg['snapshot_ttl']:
            raise ValueError('SNAPSHOT_UNKNOWN_OR_STALE: sync required')

    def _exam(self):
        """Effective verification/required_checks: the repository exam when the
        observed base commit carries one, the live machine config otherwise."""
        return {'verification': self.cfg.get('verification') or [],
                'verification_fast': self.cfg.get('verification_fast') or [],
                'required_checks': self.cfg.get('required_checks') or [],
                **self._repo_exam}

    def policy(self):
        policy = {k: self.cfg.get(k) for k in ('base', 'verification', 'required_checks',
                  'branch_prefixes', 'issue_scopes', 'policy_version')}
        policy.update(self._repo_exam)
        # Only a pinned interpreter participates in versioning: configs without
        # one stay hash-identical to pre-deploy policy, so deploying this field
        # does not rebuild every READY item once. Same zero-perturbation rule
        # for the fast tier: only a declared (non-empty) verification_fast
        # enters the policy hash.
        if self.cfg.get('interpreter'):
            policy['interpreter'] = self.cfg['interpreter']
        if not policy.get('verification_fast'):
            policy.pop('verification_fast', None)
        return policy

    def source(self, value):
        return self.store.clean_source(value)

    def _baseline_candidates(self):
        return evidence_mod.baseline_candidates(self.store, self.cfg)

    def _verification_signals(self):
        return evidence_mod.verification_signals(self.store, self.cfg)

    def _open_gate_signatures(self):
        return gate_mod.open_signatures(self.meta('gates') or {})

    def _blocked_cooldowns(self):
        return blocked_mod.cooldown_keys(self.store)

    def _mint(self, key, kind, v, payload, resources, priority,
              cooldowns, current):
        """write 事务内派生一条 work item；冷却 key 按探针节拍派 probe 变体。"""
        cd = cooldowns.get(key)
        if cd is None:
            self.store.put_item(key, kind, v, payload, resources, priority)
            current[key] = v
            return
        # 冷却 key：探针节拍到期才派生 probe 变体（评审 BLOCKER-1 解除路径）
        if blocked_mod.probe_due(time.time(), cd.get('last_created_at'),
                                 self.cfg.get('blocked_probe_seconds',
                                              blocked_mod.DEFAULT_PROBE_SECONDS)):
            probe = dict(payload, probe=True)
            pv = version(probe, self.policy())
            self.store.put_item(key, kind, pv, probe, resources, priority)
            current[key] = pv

    def _log_cooldown_flips(self, cooldowns):
        """翻转事件幂等留痕（评审 MINOR）：进入/解除才写，持续期不刷 events。"""
        for kind, payload in blocked_mod.cooldown_events(
                self.meta('blocked_cooldowns') or {}, cooldowns):
            self.store.event(kind, payload)
        self.set_meta('blocked_cooldowns', {k: v['fingerprint'] for k, v in cooldowns.items()})

    def derive(self, snapshot):
        issues = {i['number']: i for i in snapshot['issues']}
        prs = snapshot['prs']
        results = []
        def add(key, kind, payload, resources, priority=0):
            payload = self.source(payload)
            results.append((key, kind, version(payload, self.policy()), payload, resources, priority))
        window = int(time.time() // self.cfg['discovery_window_seconds'])
        discover_payload = {'task': 'DISCOVERY', 'window': window, 'base_sha': snapshot['base_sha']}
        # The key stays absent when healthy: a constant extra field would rebuild
        # the discover item's version hash — and its READY row — every sync.
        candidates = baseline.candidates(self.store.db, self.cfg)
        if candidates:
            discover_payload['baseline_failures'] = candidates
        gates = self.meta('gates') or {}
        max_attempts = int(self.cfg.get('gate_repair_max_attempts', 3))
        # Read-only here: the escalated transition is applied inside sync's
        # write transaction (derive runs outside any transaction).
        exhausted = gate_mod.escalations(gates, max_attempts)
        if exhausted:
            discover_payload['gate_escalations'] = exhausted
        self._derive_gate_items(add, gates, max_attempts, snapshot)
        add('discover', 'discover', discover_payload, ['issue-discover'], 0)
        self._derive_issues(add, issues, prs, snapshot)
        self._derive_prs(add, prs, issues, snapshot, gates)
        return results

    def _derive_gate_items(self, add, gates, max_attempts, snapshot):
        for sig, entry in sorted(gates.items()):
            if entry.get('state') != 'open' or entry.get('repair_failures', 0) >= max_attempts:
                # Escalation is a coordinator state transition, not an agent
                # ritual: after N failed repairs the signature belongs to a
                # human (applied in sync's transaction), and the gate item
                # stops minting.
                continue
            h = hashlib.sha1(sig.encode('utf-8')).hexdigest()[:12]
            payload = {'task': 'GATE_REPAIR', 'signature': sig,
                       'commands': entry.get('commands') or [], 'note': entry.get('note', ''),
                       'attempts': entry.get('attempts', 0),
                       'repair_failures': entry.get('repair_failures', 0),
                       'base_sha': snapshot['base_sha']}
            # The failure count rides the payload so every failed attempt
            # re-mints the repair item under a new version (a same-version
            # row would sit at SUCCEEDED and the gate would never retry).
            add('gate:' + h, 'handler', payload, ['gate-' + h], 15)

    def _derive_issues(self, add, issues, prs, snapshot):
        linked = linked_issues(prs)
        for number, issue in issues.items():
            if issue['state'] != 'open':
                continue
            # 打标机制是标题前缀（gitea-ops `issue tag` 把 token 前置到标题），
            # 故排除必须同时认前缀——否则打标后 `[Gap]` 前缀判定失效、决策类 issue
            # 被重新派生（与 close_issue 门禁的判定集保持一致）。
            if gate_mod._decision_pending(issue):
                continue
            related = [p for p in prs if number in linked[p['number']]]
            if any(p['state'] == 'open' for p in related):
                continue
            merged = [p for p in related if p.get('merged') and p['base']['ref'] == self.cfg['base']]
            if merged:
                add('close:%s' % number, 'handler', {'task': 'POST_MERGE_VERIFY', 'issue': issue,
                    'prs': merged, 'base_sha': snapshot['base_sha']}, ['issue-%s' % number], 5)
                continue
            scopes = self.cfg.get('issue_scopes', {}).get(str(number), ['*'])
            add('issue:%s' % number, 'handler', {'task': 'ISSUE_IMPLEMENTATION',
                'issue': issue, 'base_sha': snapshot['base_sha']},
                ['issue-%s' % number] + ['path:' + s for s in scopes], 10)

    def _derive_prs(self, add, prs, issues, snapshot, gates):
        linked = linked_issues(prs)
        signals = self._verification_signals()
        open_gates = gate_mod.open_signatures(gates)
        for pr in prs:
            if pr['state'] != 'open' or pr['base']['ref'] != self.cfg['base'] or pr.get('draft'):
                continue
            related = [issues[n] for n in linked[pr['number']] if n in issues]
            payload = {'pr': pr, 'issues': related, 'base_sha': snapshot['base_sha']}
            changes, validation_failed, approved_here = gate_mod.terminal_verdicts(
                self.store, pr, gate_mod.changes_requested(pr))
            resources = ['pr-%s' % pr['number'], 'branch-' + pr['head']['ref']]
            resources += ['issue-%s' % n for n in linked[pr['number']]]
            signal = signals.get('review:%s' % pr['number'])
            validation_base_stale = (validation_failed
                                     and gate_mod.attributed_stale(signal, pr))
            validation_frozen = (validation_failed
                                 and gate_mod.is_frozen(signal, pr, open_gates,
                                                        snapshot['base_sha']))
            repair_reasons = []
            if changes:
                repair_reasons.append('changes_requested')
            if validation_failed and not validation_frozen:
                repair_reasons.append('validation_failed')
            if pr.get('mergeable') is False:
                repair_reasons.append('merge_conflict')
            if validation_base_stale:
                payload['task'] = 'PR_SYNC_BASE'
                payload['repair_reasons'] = repair_reasons
                add('repair:%s' % pr['number'], 'handler', payload,
                    resources + ['path:*'], 20)
            elif validation_frozen:
                # Suppress only the validation-derived repair. Independent
                # review and merge-conflict work remains actionable.
                if not (changes or pr.get('mergeable') is False):
                    continue
                payload['repair_reasons'] = repair_reasons
                payload['task'] = ('PR_REVIEW_FIX' if changes
                                   else 'PR_CONFLICT_RESOLUTION')
                add('repair:%s' % pr['number'], 'handler', payload,
                    resources + ['path:*'], 20)
            elif repair_reasons:
                payload['task'] = ('PR_REVIEW_FIX' if changes or validation_failed
                                   else 'PR_CONFLICT_RESOLUTION')
                payload['repair_reasons'] = repair_reasons
                add('repair:%s' % pr['number'], 'handler', payload, resources + ['path:*'], 20)
            elif gate_mod.attributed_stale(signal, pr):
                # The red survived the rerun and pre-exists at the fork point
                # while the base tip is green: merging origin/base cures it.
                payload['task'] = 'PR_SYNC_BASE'
                add('repair:%s' % pr['number'], 'handler', payload, resources + ['path:*'], 20)
            elif approved_here:
                continue
            elif gate_mod.is_frozen(signal, pr, open_gates, snapshot['base_sha']):
                # The gate cannot produce a signal about this PR until its
                # repair lands; deriving the review item again would only buy
                # another Blocked one-liner per cycle.
                continue
            else:
                gate_mod.attach_gate_state(payload, signal, pr, gates)
                payload['task'] = 'PR_REVIEW'
                add('review:%s' % pr['number'], 'reviewer', payload, resources, 0)

    def _project_key(self):
        key = self.cfg.get('project_key')
        if not key:
            raise ValueError('PROJECT_KEY_REQUIRED: observer store needs the project key')
        return key

    @staticmethod
    def _review_source_id(review):
        review_id = review.get('id')
        if review_id is None:
            identity = {'user': (review.get('user') or {}).get('id'),
                        'state': review.get('state'),
                        'submitted_at': review.get('submitted_at'),
                        'commit_id': review.get('commit_id'),
                        'body_sha': hashlib.sha256(
                            (review.get('body') or '').encode('utf-8')).hexdigest()}
            review_id = hashlib.sha256(dump(identity).encode('utf-8')).hexdigest()
        return 'github-review:%s' % review_id

    def _repair_event_candidates(self, previous, snapshot, previous_synced_at):
        """Return newly observed repair sources grouped by PR.

        First observation establishes a baseline and deliberately emits no
        historical PR work. Internal SENT reviewer operations are baselined
        the same way through event_sources.
        """
        grouped = {}
        outbox_sources_to_remember = []
        project = self._project_key()
        current = {pr['number']: pr for pr in snapshot.get('prs', [])}
        old = {pr['number']: pr for pr in (previous or {}).get('prs', [])}

        def eligible(pr):
            return (pr.get('state') == 'open'
                    and (pr.get('base') or {}).get('ref') == self.cfg['base']
                    and not pr.get('draft'))

        def add(number, reason, source_id):
            entry = grouped.setdefault(number, {'reasons': set(), 'source_ids': set()})
            entry['reasons'].add(reason)
            entry['source_ids'].add(source_id)

        if previous is not None:
            for number, pr in current.items():
                if not eligible(pr):
                    continue
                prior = old.get(number)
                if prior is None:
                    continue
                prior_ids = {self._review_source_id(review)
                             for review in prior.get('reviews', [])}
                for review in pr.get('reviews', []):
                    state = (review.get('state') or '').upper()
                    source_id = self._review_source_id(review)
                    if (source_id not in prior_ids and state in
                            ('REQUEST_CHANGES', 'REQUESTED_CHANGES',
                             'CHANGES_REQUESTED')):
                        add(number, 'changes_requested', source_id)
                head_changed_during_conflict = (
                    prior.get('mergeable') is False
                    and (((prior.get('head') or {}).get('sha')
                          != (pr.get('head') or {}).get('sha'))
                         or ((previous or {}).get('base_sha')
                             != snapshot.get('base_sha'))))
                if (pr.get('mergeable') is False
                        and (prior.get('mergeable') is not False
                             or head_changed_during_conflict)):
                    edge = 'merge-conflict:%s:%s:%s:%s:%s' % (
                        project, number, (pr.get('head') or {}).get('sha', ''),
                        snapshot.get('base_sha', ''), repr(previous_synced_at))
                    add(number, 'merge_conflict', edge)

        for row in self.store.db.execute(
                "SELECT id,payload FROM outbox WHERE action='review_comment' "
                "AND state='SENT'"):
            source_id = 'reviewer-verdict:%s' % row['id']
            if self.store.event_source_seen(source_id):
                continue
            try:
                body = json.loads(row['payload'])
            except ValueError:
                outbox_sources_to_remember.append(source_id)
                continue
            match = re.search(r'Decision: Changes requested', body.get('body') or '')
            if match and previous is not None:
                pr = current.get(body.get('number'))
                if (pr and eligible(pr)
                        and ('Head: ' + pr['head']['sha']) in body.get('body', '')):
                    add(pr['number'], 'changes_requested', source_id)
                    continue
            # A SENT operation is immutable. Mark irrelevant decisions as
            # observed too, so every sync does not rescan old approvals and
            # validation failures forever.
            outbox_sources_to_remember.append(source_id)

        events = []
        for number, entry in sorted(grouped.items()):
            pr = current[number]
            source_ids = sorted(entry['source_ids'])
            event_key = hashlib.sha256(
                (project + '|' + '|'.join(source_ids)).encode('utf-8')).hexdigest()
            events.append({
                'source_ids': source_ids,
                'payload': {
                    'kind': 'pr.handler_repair_needed', 'entity': 'pr:%s' % number,
                    'source_event_id': 'pr-repair:' + event_key,
                    'project': project, 'number': number,
                    'head_sha': (pr.get('head') or {}).get('sha'),
                    'base_ref': (pr.get('base') or {}).get('ref'),
                    'base_sha': snapshot.get('base_sha'),
                    'reasons': sorted(entry['reasons'])}})
        return events, outbox_sources_to_remember

    def sync(self):
        try:
            # Facts come from the shared observer store (one crawl path for
            # the CP and the dashboard); a store miss or an aged entry crawls
            # inline here, so a sync is never wrong — at worst it is slow.
            # Fetch outside the write transaction: a slow remote must not hold
            # BEGIN IMMEDIATE and block claims. Each sync still commits its full
            # observation atomically, so a partial snapshot is never visible.
            snapshot = observer.snapshot(observer.default_db_path(), self._project_key(),
                                         self.cfg['snapshot_ttl'], self.platform.snapshot)
            if not isinstance(snapshot, dict) or not all(k in snapshot for k in ('issues', 'prs', 'base_sha')):
                raise ValueError('INVALID_SNAPSHOT')
            # The exam is resolved before derive(): item versions hash the policy, so
            # the snapshot and its judged exam enter the store together.
            exam_record = exam.resolve(self.platform, self.meta('exam'), snapshot['base_sha'])
            self._repo_exam = exam.overlay(exam_record)
            previous_snapshot = self.meta('snapshot')
            previous_synced_at = self.meta('synced_at', 0)
            items = self.derive(snapshot)
            repair_events, outbox_sources_to_remember = self._repair_event_candidates(
                previous_snapshot, snapshot, previous_synced_at)
            with self.store.transaction():
                # Escalation transition inside the write transaction: after N
                # failed repairs the signature belongs to a human and stops
                # minting (derive's read above already skipped it).
                max_attempts = int(self.cfg.get('gate_repair_max_attempts', 3))
                gates = self.meta('gates') or {}
                if gate_mod.escalate(gates, max_attempts):
                    self.set_meta('gates', gates)
                previous = self.meta('exam') or {}
                current = {}
                # blocked 历史与派生决策同事务同事实（issue #280）
                cooldowns = self._blocked_cooldowns()
                self._log_cooldown_flips(cooldowns)
                for key, kind, v, payload, resources, priority in items:
                    self._mint(key, kind, v, payload, resources, priority,
                               cooldowns, current)
                for event in repair_events:
                    self.store.event_with_sources(
                        'pr.handler_repair_needed', event['payload'],
                        event['source_ids'])
                for source_id in outbox_sources_to_remember:
                    self.store.remember_event_source(source_id)
                for old in self.store.items():
                    if old['state'] == 'READY' and current.get(old['key']) != old['version']:
                        self.store.db.execute("UPDATE work_items SET state='CANCELLED' WHERE id=?", (old['id'],))
                self.set_meta('snapshot', snapshot)
                self.set_meta('exam', exam_record)
                self.set_meta('current_versions', current)
                self.set_meta('synced_at', time.time())
                self.set_meta('sync_error', None)
                if previous.get('source') not in (None, exam_record.get('source')):
                    self.store.event('exam.source', {'source': exam_record.get('source'),
                                                     'base_sha': exam_record['base_sha']})
                self.store.event('sync.completed', {'items': len(items)})
            return {'items': len(items)}
        except Exception as exc:
            self.set_meta('sync_error', type(exc).__name__)
            # The type name alone cannot tell a broken exam from a platform
            # outage; the message carries the EXAM_* code on that path.
            self.store.event('sync.failed', {'error': type(exc).__name__,
                                             'detail': str(exc)[:200]})
            raise

    def evaluate_worker_stalls(self, machines, threshold=64800, now=None):
        """Persist machine/project discover stall and recovery edges.

        ``machines`` is the fresh registry projection for this project's
        grants. A worker with no prior discover claim is not called stalled;
        there is no schedule history to distinguish a new install from a
        missed run.
        """
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) \
                or threshold <= 0:
            raise ValueError('WORKER_STALL_THRESHOLD_INVALID')
        now = time.time() if now is None else now
        project = self._project_key()
        last_by_machine = {}
        rows = self.store.db.execute(
            "SELECT e.id,e.worker,e.created_at,e.state,e.expires_at "
            "FROM executions e JOIN work_items w ON w.id=e.item_id "
            "WHERE w.kind='discover' ORDER BY e.created_at,e.id").fetchall()
        for row in rows:
            worker = row['worker'] or ''
            name = worker.split('/', 1)[0]
            try:
                created = float(row['created_at'])
            except (TypeError, ValueError):
                continue
            current = last_by_machine.get(name)
            if current is None or (created, row['id']) > (current['created_at'], current['id']):
                last_by_machine[name] = dict(row, created_at=created)

        previous = self.meta('worker_stalls') or {}
        current_state = {}
        events = []
        known_keys = set()
        for machine in machines or []:
            machine_id, name = machine.get('id'), machine.get('name')
            if not machine_id or not name:
                continue
            key = '%s|discover' % machine_id
            known_keys.add(key)
            prior = previous.get(key)
            execution = last_by_machine.get(name)
            eligible = (bool(machine.get('enabled')) and not machine.get('revoked')
                        and bool(machine.get('granted')))
            age = max(0.0, now - execution['created_at']) if execution else None
            stalled = bool(eligible and execution and age > threshold)
            if stalled:
                source_id = 'worker-stall:%s:%s:discover:%s' % (
                    machine_id, project, execution['id'])
                state = {'machine_id': machine_id, 'machine': name,
                         'project': project, 'loop': 'discover',
                         'last_execution_id': execution['id'],
                         'last_activity': execution['created_at'],
                         'source_id': source_id}
                current_state[key] = state
                if prior is None or prior.get('last_execution_id') != execution['id']:
                    payload = dict(state, kind='worker.loop_stalled',
                                   entity='worker:%s:discover' % machine_id,
                                   age_seconds=age, threshold_seconds=threshold,
                                   execution_state=execution['state'])
                    payload['source_event_id'] = 'worker-stall:' + hashlib.sha256(
                        source_id.encode('utf-8')).hexdigest()
                    events.append(('worker.loop_stalled', payload, [source_id]))
            elif prior is not None:
                resolution = 'worker-stall-resolved:%s:%s:discover:%s:%s' % (
                    machine_id, project, prior.get('last_execution_id'),
                    execution['id'] if execution else 'unobserved')
                payload = {'kind': 'worker.loop_resolved',
                           'entity': 'worker:%s:discover' % machine_id,
                           'source_event_id': 'worker-stall-resolved:' + hashlib.sha256(
                               resolution.encode('utf-8')).hexdigest(),
                           'machine_id': machine_id, 'machine': name,
                           'project': project, 'loop': 'discover',
                           'last_execution_id': execution['id'] if execution else None,
                           'age_seconds': age, 'threshold_seconds': threshold,
                           'reason': ('within_threshold' if eligible
                                      else 'machine_or_project_not_dispatchable')}
                events.append(('worker.loop_resolved', payload, [resolution]))

        # A removed/revoked machine is still resolved even if it no longer
        # appears in the current grant projection.
        for key, prior in previous.items():
            if key in known_keys:
                continue
            machine_id = prior.get('machine_id')
            source_id = 'worker-stall-resolved:%s:%s:discover:%s:unregistered' % (
                machine_id, project, prior.get('last_execution_id'))
            payload = {'kind': 'worker.loop_resolved',
                       'entity': 'worker:%s:discover' % machine_id,
                       'source_event_id': 'worker-stall-resolved:' + hashlib.sha256(
                           source_id.encode('utf-8')).hexdigest(),
                       'machine_id': machine_id, 'machine': prior.get('machine'),
                       'project': project, 'loop': 'discover',
                       'last_execution_id': prior.get('last_execution_id'),
                       'age_seconds': None, 'threshold_seconds': threshold,
                       'reason': 'machine_or_project_not_registered'}
            events.append(('worker.loop_resolved', payload, [source_id]))

        with self.store.transaction():
            for kind, payload, source_ids in events:
                self.store.event_with_sources(kind, payload, source_ids)
            self.set_meta('worker_stalls', current_state)
        return {'stalled': sum(1 for value in current_state.values()
                               if value.get('source_id')),
                'events': len(events)}

    def claim(self, loop, worker=None, item_id=None, ttl=None):
        # No registry names this machine in the in-process personality, so an
        # omitted (or blank) worker composes the whole <hostname>/<loop> label.
        worker = (worker or '').strip() or default_worker(loop)
        self.enabled()
        if loop not in ('discover', 'handler', 'reviewer'):
            raise ValueError('UNKNOWN_LOOP')
        # A claim without fresh facts is meaningless: refresh a stale snapshot
        # inline (same pattern as report's built-in re-sync) — and only then
        # judge the selector, so an item the next sync would mint is not
        # rejected as unknown.
        if self.meta('sync_error') or time.time() - self.meta('synced_at', 0) > self.cfg['snapshot_ttl']:
            self.sync()
        if item_id is not None:
            known = self.store.db.execute('SELECT 1 FROM work_items WHERE id=? OR key=?',
                                          (item_id, item_id)).fetchone()
            if known is None:
                raise ValueError('ITEM_UNKNOWN: claim --item takes a full item id or key '
                                 '(see list); a wrong selector must never look like an empty queue')
        # Reclaim expired executions before the queue check so a crashed round
        # cannot wedge candidates behind its resources. Silent corpses (never
        # a protocol call past claim) are collapsed first: their slot must not
        # stay hostage to the full lease. A config without the key opts out.
        self.store.collapse_idle(self.cfg.get('orphan_idle_seconds', 0))
        self.store.sweep_expired()
        return self.store.claim(loop, worker,
                                self.cfg['lease_seconds'] if ttl is None else ttl,
                                self.cfg['max_parallel'], item_id)

    def claim_raw(self, loop, worker=None, item_id=None, ttl=None):
        """The claim envelope the server route composes (server.py): the
        execution plus its item row and policy on success, and on a refusal
        the holder snapshot (`blocked_by`) so a BLOCKED round reports who
        until when instead of a bare null. The local carrier and the remote
        WorkerService then speak one shape."""
        execution = self.claim(loop, worker, item_id, ttl)
        if execution is None:
            return {'execution': None, 'item': None,
                    'policy': self.policy(), 'blocked_by': self.store.last_blocked}
        item = dict(self.store.db.execute('SELECT * FROM work_items WHERE id=?',
                                          (execution['item_id'],)).fetchone())
        return {'execution': execution, 'item': item, 'policy': self.policy()}

    def abandon(self, eid, token):
        """Holder-side abandonment (not a loop verb, not exposed to sessions):
        the dashboard's pre-claim calls it when the round it claimed could not
        be handed off (worktree/bind/spawn failure), so the item returns to the
        queue now instead of after the full lease. Deliberately not gated on
        ``enabled()``: refusing cleanup on a disabled machine would strand the
        claim. Every effect flows through the standard sweep derivation."""
        return self.store.abandon(eid, token)

    def execution(self, eid, token, current=True):
        self.enabled()
        e = self.store.guard(eid, token)
        item = dict(self.store.db.execute('SELECT * FROM work_items WHERE id=?', (e['item_id'],)).fetchone())
        if current:
            self.fresh()
            if self.meta('current_versions', {}).get(item['key']) != e['item_version']:
                self._cancel_stale(e, item)
                raise ValueError('STALE_INPUT: source changed; execution cancelled — '
                                 'reclaim the new version on your next claim')
        return e, item, json.loads(item['payload'])

    def _cancel_stale(self, e, item):
        return self.store.cancel_stale(e, item)

    def bind(self, eid, token, worktree, branch=None):
        root = self.cfg.get('repo_root')
        if branch is None and root and Path(worktree).resolve() == Path(root).resolve():
            # GATE_REPAIR: the repair effect lives in the project checkout,
            # not a worktree — attest the checkout instead of isolating it.
            context = {'worktree': str(Path(worktree).resolve()), 'branch': None,
                       'head': git(worktree, 'rev-parse', 'HEAD')}
        else:
            context = validate_worktree(self.cfg, worktree, branch)
        return self.bind_checked(eid, token, context)

    def _bind_gate_repair(self, eid, token, payload, context):
        """Machine-local repair: no branch lease, no worktree isolation. The
        worker attests the checkout path and head; the whitelisted gate
        commands are the only sanctioned effect."""
        if not re.fullmatch(r'[0-9a-f]{40}', context.get('head') or ''):
            raise ValueError('PR_HEAD_MISMATCH')
        with self.store.transaction():
            self.store.guard(eid, token)
            self.store._renew_locked(eid, self.cfg['lease_seconds'])
            self.store.db.execute('UPDATE executions SET worktree=?,branch=?,head=? WHERE id=?',
                                  (context.get('worktree'), None, context.get('head'), eid))
            self.store.event('execution.bound', dict(context, execution_id=eid))
        return dict(context, task='GATE_REPAIR', gate_commands=payload.get('commands') or [])

    def _bind_pr_guards(self, item, payload, context):
        if payload.get('task') == 'POST_MERGE_VERIFY' and context['head'] != payload['base_sha']:
            raise ValueError('POST_MERGE_BASE_SHA_MISMATCH')
        if not (payload.get('pr') and (item['kind'] == 'reviewer'
                                       or payload['task'].startswith('PR_'))):
            return
        if item['kind'] == 'reviewer' and context['head'] != payload['pr']['head']['sha']:
            raise ValueError('PR_HEAD_MISMATCH')
        if item['kind'] != 'handler':
            return
        if payload['task'] in ('PR_CONFLICT_RESOLUTION', 'PR_SYNC_BASE', 'PR_REVIEW_FIX'):
            # The original PR branch may be occupied by a foreign worktree
            # (git allows one worktree per branch); a temp repair branch is
            # acceptable when it starts at the reviewed head — push targets
            # the PR branch either way. Review-fix hits the same deadlock
            # (issue #279): publish pushes back to the original PR ref via
            # refspec, so forcing the original branch checkout only blocks
            # tasks whose branch is legitimately occupied.
            if (context['branch'] != payload['pr']['head']['ref']
                    and context['head'] != payload['pr']['head']['sha']):
                raise ValueError('PR_HEAD_MISMATCH')
        elif context['branch'] != payload['pr']['head']['ref']:
            raise ValueError('ORIGINAL_PR_BRANCH_REQUIRED')

    def bind_checked(self, eid, token, context):
        """Coordinator half of bind: the worktree was validated where it lives;
        this side re-checks every data fact and reserves the branch lease."""
        e, item, payload = self.execution(eid, token)
        if item['kind'] == 'discover':
            raise ValueError('DISCOVERY_IS_READ_ONLY')
        if payload.get('task') == 'GATE_REPAIR':
            return self._bind_gate_repair(eid, token, payload, context)
        self._bind_pr_guards(item, payload, context)
        if item['kind'] == 'handler' and payload['task'] != 'POST_MERGE_VERIFY' and not context['branch']:
            raise ValueError('DEVELOPMENT_BRANCH_REQUIRED')
        # Branch must have been reserved, or reserve it before allowing task writes.
        resources = json.loads(item['resources'])
        resource = 'branch-' + context['branch'] if context['branch'] else None
        with self.store.transaction():
            self.store.guard(eid, token)
            self.store._renew_locked(eid, self.cfg['lease_seconds'])
            if resource and resource not in resources:
                if self.store.db.execute('SELECT 1 FROM leases WHERE resource=?', (resource,)).fetchone():
                    raise ValueError('BRANCH_LOCKED')
                # Legacy lock also blocks; takeover only reclaims provably
                # dead owners (terminal in this store), never unknown ones.
                owner = {'execution_id': eid, 'token': token}
                if self.store._acquire_lock_file(resource, owner) is None:
                    raise ValueError('BRANCH_LOCKED')
                resources.append(resource)
                renewed = self.store.db.execute('SELECT expires_at FROM executions WHERE id=?',
                                                (eid,)).fetchone()
                self.store.db.execute('INSERT INTO leases VALUES (?,?,?,?)',
                                      (resource, eid, token, renewed['expires_at']))
                self.store.db.execute('UPDATE work_items SET resources=? WHERE id=?', (dump(resources), item['id']))
            self.store.db.execute('UPDATE executions SET worktree=?,branch=?,head=? WHERE id=?',
                                  (context['worktree'], context['branch'], context['head'], eid))
            self.store.event('execution.bound', dict(context, execution_id=eid))
        # kind 随 bind 应答落进 worker 的 bind context：worker 本地 verify 据此
        # 走 verification_fast / verification 分层（resolve_verification）。
        return dict(context, kind=item['kind'])

    def _bound_execution(self, eid, token):
        """Claim-scoped facts for evidence filing: identity and lease only.
        Evidence is a local, head-stamped fact — it does not expire — so
        verify never bounces completed gate work on snapshot age or input
        churn; input currency is enforced at the consumption points
        (report/publish), which carry their own guards and self-heal."""
        self.enabled()
        e = self.store.guard(eid, token)
        item = dict(self.store.db.execute('SELECT * FROM work_items WHERE id=?',
                                          (e['item_id'],)).fetchone())
        return e, item, json.loads(item['payload'])

    def verify(self, eid, token):
        e, item, payload = self._bound_execution(eid, token)
        if not e['worktree'] or item['kind'] == 'discover':
            raise ValueError('BOUND_WORKTREE_REQUIRED')
        if payload.get('task') == 'GATE_REPAIR':
            context = {'worktree': e['worktree'], 'branch': e['branch'], 'head': e['head']}
            result = run_gate_commands(self.cfg, payload.get('commands') or [], e['worktree'],
                                       Path(self.cfg['state_dir']) / 'executions' / eid / str(time.time_ns()))
            return self._record_verification(e, item, payload, context, result)
        context = validate_worktree(self.cfg, e['worktree'], e['branch'])
        self._guard_reviewer_head(item, payload, context)
        cfg = dict(self.cfg, **self._exam())
        cfg['verification'] = resolve_verification(cfg, item['kind'])
        result = run_checks(cfg, context,
                            Path(self.cfg['state_dir']) / 'executions' / eid / str(time.time_ns()))
        return self._record_verification(e, item, payload, context, result)

    def record_verification(self, eid, token, context, checks):
        """Coordinator half of verify: checks ran on the worker, this side
        guards, stamps the authoritative input version, and files evidence."""
        e, item, payload = self._bound_execution(eid, token)
        if not e['worktree'] or item['kind'] == 'discover':
            raise ValueError('BOUND_WORKTREE_REQUIRED')
        self._guard_reviewer_head(item, payload, context)
        return self._record_verification(e, item, payload, context, checks)

    def _guard_reviewer_head(self, item, payload, context):
        # Original order: a stale reviewer head must fail before any check runs.
        if item['kind'] == 'reviewer' and context['head'] != payload['pr']['head']['sha']:
            raise ValueError('PR_HEAD_MISMATCH')

    def _record_verification(self, e, item, payload, context, result):
        result = dict(result)
        result['input_version'] = e['item_version']
        eid, token = e['id'], e['token']
        if result.get('env_signature'):
            with self.store.transaction():
                result['prior_env_failures'] = evidence_mod.count_env_failure(
                    self.store, result, eid, self.meta, self.set_meta)
        else:
            result['prior_env_failures'] = 0
        self.store.add_evidence(eid, token, 'verification', result)
        if result.get('passed'):
            # A green run from an item that previously failed on signature S
            # is proof the gate is healthy again — resolve S even if the
            # formal repair item never reported (e.g. the environment was
            # fixed out-of-band after escalation).
            prior = evidence_mod.prior_env_signatures(self.store, item['id'], eid)
            self._write_gates(lambda gates: gate_mod.resolve_signatures(gates, prior))
        self.store.db.execute('UPDATE executions SET head=? WHERE id=?', (context['head'], eid))
        self.store.renew(eid, token, self.cfg['lease_seconds'])
        return result

    def report(self, eid, token, result):
        try:
            e, item, payload = self.execution(eid, token)
        except ValueError as exc:
            if not str(exc).startswith('SNAPSHOT_UNKNOWN_OR_STALE'):
                raise
            # #66: 长轮次（Inspection+Verification 常 >900s）快照超 snapshot_ttl——
            # report 内建自动 re-sync，避免 agent 手重试 sync 被拒、误判失败。
            self.sync()
            e, item, payload = self.execution(eid, token)  # fresh() 现通过
        gate = gate_mod.validate_report(self, e, item, payload, eid, result)
        action = result.get('action')
        if action or result.get('actions'):
            self.authorize(item, payload, result)
        with self.store.transaction():
            self.store.guard(eid, token)
            if self.store.db.execute('SELECT 1 FROM outbox WHERE execution_id=?', (eid,)).fetchone():
                raise ValueError('PUBLICATION_ALREADY_STARTED: report is frozen')
            self.store.db.execute('UPDATE executions SET result=? WHERE id=?', (dump(result), eid))
            self.store.event('execution.reported', {'execution_id': eid, 'decision': result['decision']})
            self.store._renew_locked(eid, self.cfg['lease_seconds'])
        self.store.add_evidence(eid, token, 'judgment', dict(result, input_version=e['item_version'], head=e['head']))
        # Gate ledger transitions — the exit edges of "cannot judge".
        # Only env-class gates register here: stale-branch exits through the
        # sync-base repair edge, base-defect through the discover baseline
        # lane — both derive from verification evidence, not this ledger.
        if (item['kind'] == 'reviewer' and result['decision'] == 'Blocked'
                and gate is not None and gate.get('signature')):
            self._write_gates(lambda gates: gate_mod.register_blocked(gates, gate))
        if item['kind'] == 'handler' and payload.get('task') == 'GATE_REPAIR':
            self._write_gates(lambda gates: gate_mod.apply_repair_result(
                gates, payload.get('signature'), result['decision']))
        # D1: a report that requested no publication IS the round's last step —
        # the terminal state derives from the delivered facts, no finish ritual.
        if not (result.get('action') or result.get('actions')) and self.store.derive_terminal(eid) is not None:
            return dict(result, execution_state='SUCCEEDED')
        return dict(result, execution_state='ACTIVE')

    def _validate_report(self, result, e, item, payload):
        if not isinstance(result, dict) or set(result) - {'decision', 'reviewer', 'summary', 'action', 'payload', 'coverage'}:
            raise ValueError('INVALID_REPORT_FIELDS')
        if not all(isinstance(result.get(k), str) and result[k].strip() for k in ('decision', 'reviewer', 'summary')):
            raise ValueError('DECISION_REVIEWER_SUMMARY_REQUIRED')
        if result['reviewer'] == e['worker']:
            raise ValueError('INDEPENDENT_REVIEW_REQUIRED')
        if item['kind'] == 'reviewer' and payload.get('pr'):
            developers = self.store.db.execute('''SELECT e.worker FROM executions e JOIN work_items w ON w.id=e.item_id
                 WHERE w.kind='handler' AND e.branch=?''', (payload['pr']['head']['ref'],)).fetchall()
            if any(r['worker'] in (e['worker'], result['reviewer']) for r in developers):
                raise ValueError('DEVELOPER_CANNOT_ACCEPT_OWN_PR')
        expected = {'discover': ('CONFIRMED', 'REJECTED', 'NEEDS_MORE_EVIDENCE'), 'handler': ('APPROVE', 'REQUEST_CHANGES', 'BLOCKED', 'RESOLVED'), 'reviewer': ('Approved for manual merge', 'Changes requested', 'Blocked', 'Validation failed')}
        if result['decision'] not in expected[item['kind']]:
            raise ValueError('INVALID_DECISION')
        self._validate_coverage(item, result)
        self._validate_blocked_visibility(item, payload, result)

    def _validate_blocked_visibility(self, item, payload, result):
        # BLOCKED conflict resolution must carry its visibility comment (handler.md: 非静默红线).
        if (item['kind'] == 'handler' and result['decision'] == 'BLOCKED'
                and payload.get('task') == 'PR_CONFLICT_RESOLUTION'
                and result.get('action') != 'comment_pr'):
            raise ValueError('BLOCKED_VISIBILITY_REQUIRED')

    def _validate_coverage(self, item, result):
        coverage = result.get('coverage')
        if coverage is not None:
            validate_coverage(coverage)
            if item['kind'] == 'reviewer' and result['decision'] == 'Approved for manual merge' and not coverage['ok']:
                raise ValueError('COVERAGE_INCOMPLETE')
        elif item['kind'] == 'reviewer' and result['decision'] == 'Approved for manual merge':
            raise ValueError('COVERAGE_REQUIRED')

    def authorize(self, item, source, result):
        return gate_mod.authorize(self, item, source, result)

    def gate(self, e, item, source, result, context=None):
        return gate_mod.publish_gate(self, e, item, source, result, context)

    def publish(self, eid, token):
        e, item, source = self.execution(eid, token, current=False)
        result = json.loads(e['result']) if e['result'] else {}
        if result.get('actions') is not None:
            return self._publish_actions(eid, token, result)
        prepared = self.prepare_publish(eid, token)
        if prepared['receipt'] is not None:
            return prepared['receipt']
        if prepared['push_required']:
            try:
                self.push(prepared['execution'], prepared)
            except Exception:
                self.mark_publish_unknown(prepared['op'])
                raise ValueError('PUBLICATION_UNKNOWN: query receipt before any retry') from None
        return self.complete_publish(eid, token, prepared['op'], pushed=True)

    def _publish_actions(self, eid, token, result):
        """Local carrier for a batch; remote workers use begin/commit per item."""
        actions = result['actions']
        if not isinstance(actions, list) or not 2 <= len(actions) <= 10:
            raise ValueError('INVALID_ACTIONS')
        receipts = []
        for index in range(len(actions)):
            prepared = self.prepare_publish(eid, token, action_index=index)
            if prepared.get('receipt') is not None:
                receipts.append(prepared['receipt'])
            else:
                receipts.append(self.complete_publish(eid, token, prepared['op'], pushed=True))
        self.store.derive_terminal(eid)
        return {'issues': receipts, 'execution_state': 'SUCCEEDED'}

    def _exam_guard(self, action, source, payload):
        """Exam guard (spec B5): stamp reviewer verdicts of PRs touching the
        exam path with the fixed note and record the observational event; the
        agent cannot add or remove the line. Runs before the outbox INSERT, so
        a files-query failure raises clean (no SENDING residue) for retry."""
        if action not in ('review_comment', 'comment_pr'):
            return
        if EXAM_PATH in self.platform.pr_files(source['pr']['number']):
            payload['body'] += '\n' + exam.GUARD_NOTE
            self.store.event('exam.touched_by_pr', {'pr': source['pr']['number'], 'head': source['pr']['head']['sha']})

    def prepare_publish(self, eid, token, context=None, action_index=None):
        """First half of publication: reconcile past attempts, gate, and record
        the outbox intent before any external effect. Returns the operation the
        worker must confirm via complete_publish (pushing first when required)."""
        e, item, source = self.execution(eid, token, current=False)
        if not e['result']:
            raise ValueError('REPORT_REQUIRED')
        result = json.loads(e['result'])
        if not result.get('action'):
            if result.get('actions') is not None:
                return self._prepare_batch_action(eid, token, e, item, source,
                                                  result, action_index, context)
            raise ValueError('NO_PUBLICATION_REQUESTED')
        op = version([eid, result], {})
        marker = '<!-- code-loop:' + op + ' -->'
        row = self.store.db.execute('SELECT * FROM outbox WHERE id=?', (op,)).fetchone()
        if row and row['state'] == 'SENT':
            # Crash-recovery: the publication landed, so the round is already
            # complete — derive the terminal state instead of awaiting a sweep.
            self.store.derive_terminal(eid)
            return {'op': op, 'receipt': json.loads(row['receipt']), 'push_required': False}
        if row and row['state'] in ('SENDING', 'UNKNOWN'):
            # Never resend after ambiguous completion. Query is safe even if another sender is alive.
            receipt = self.platform.find(marker, row['action'], json.loads(row['payload']))
            if receipt is None:
                raise ValueError('PUBLICATION_UNKNOWN: no receipt; do not resend')
            with self.store.transaction():
                self.store.guard(eid, token)
                self.store.db.execute("UPDATE outbox SET state='SENT',receipt=? WHERE id=?", (dump(receipt), op))
                self.store._renew_locked(eid, self.cfg['lease_seconds'])
                resources = self.store.finalize_locked(eid)
            self._release_after_finalize(eid, token, resources)
            return {'op': op, 'receipt': receipt, 'push_required': False}
        self.sync()
        e, item, source = self.execution(eid, token)
        self.gate(e, item, source, result, context)
        action = result['action']
        p = dict(result.get('payload', {}))
        if action == 'create_pr':
            if p.get('head') != e['branch'] or p.get('base') != self.cfg['base']:
                raise ValueError('PR_BRANCH_BASE_MISMATCH')
            if not re.search(r'(?i)\bcloses\s+#%s\b' % source['issue']['number'], p.get('body', '')):
                raise ValueError('CLOSES_REFERENCE_REQUIRED')
            if any(pr['state']=='open' and pr['head']['ref']==e['branch'] for pr in self.snapshot()['prs']):
                raise ValueError('PR_ALREADY_EXISTS')
        if action == 'review_comment':
            p['body'] = ('Decision: ' + result['decision'] + '\nHead: ' + source['pr']['head']['sha']
                         + '\nBase: ' + source['base_sha']
                         + coverage_line(result.get('coverage'))
                         + '\n' + result['summary'] + '\n' + p.get('body', ''))
        if action == 'comment_pr':
            # Blocked rounds carry the reviewer-verdict word so the dashboard's
            # comment channel recognizes the PR as blocked.
            p['body'] = ('Decision: ' + {'BLOCKED': 'Blocked'}[result['decision']]
                         + '\nHead: ' + source['pr']['head']['sha']
                         + '\nBase: ' + source['base_sha'] + '\n' + result['summary'] + '\n' + p.get('body', ''))
        self._exam_guard(action, source, p)
        if 'body' in p:
            # Identity signature: human-readable line on top, machine marker at
            # the end, so users and the dashboard can tell agent writes apart
            # from human comments posted under the same platform account.
            p['body'] = ('> 🤖 本条由 code-loop **%s** 循环自动生成\n\n' % item['kind']
                         + p['body'] + '\n\n' + marker
                         + '\n<!-- code-loop-agent: %s -->' % item['kind'])
        with self.store.transaction():
            self.store.guard(eid, token)
            if self.store.db.execute('SELECT 1 FROM outbox WHERE id=?', (op,)).fetchone():
                raise ValueError('PUBLICATION_IN_PROGRESS: retry to reconcile')
            self.store.db.execute('INSERT INTO outbox VALUES (?,?,?,?,?,NULL)', (op, eid, action, dump(p), 'SENDING'))
            self.store._renew_locked(eid, self.cfg['lease_seconds'])
        # Repair (conflict / sync-base / review-fix) may run on a temp
        # branch when a foreign worktree occupies the original PR branch
        # (git allows one worktree per branch); the push always lands on the
        # original PR branch. Resolved here, where the authoritative payload
        # lives, so both the local and worker push paths can consume it.
        target = e['branch']
        if source.get('task') in ('PR_CONFLICT_RESOLUTION', 'PR_SYNC_BASE',
                                  'PR_REVIEW_FIX') and source.get('pr'):
            target = source['pr']['head']['ref']
        return {'op': op, 'receipt': None, 'push_required': action in ('create_pr', 'edit_pr'),
                'action': action, 'payload': p, 'target_branch': target,
                'execution': {'worktree': e['worktree'], 'branch': e['branch'], 'head': e['head']}}

    def _prepare_batch_action(self, eid, token, execution, item, source, result,
                              action_index, context=None):
        """Persist or reconcile one child Issue operation for publish/begin."""
        actions = result['actions']
        if item['kind'] != 'discover' or not isinstance(actions, list) or not 2 <= len(actions) <= 10:
            raise ValueError('INVALID_ACTIONS')
        if action_index is None:
            action_index = 0
        if (isinstance(action_index, bool) or not isinstance(action_index, int)
                or action_index < 0 or action_index >= len(actions)):
            raise ValueError('INVALID_ACTION_INDEX')
        entry = actions[action_index]
        if entry.get('action') != 'create_issue':
            raise ValueError('BATCH_ACTION_FORBIDDEN')
        op = version([eid, 'report-action', action_index], {})
        marker = '<!-- code-loop:' + op + ' -->'
        row = self.store.db.execute('SELECT * FROM outbox WHERE id=?', (op,)).fetchone()
        if row is not None and row['state'] == 'SENT':
            self.store.derive_terminal(eid)
            return {'op': op, 'receipt': json.loads(row['receipt']), 'push_required': False,
                    'batch_index': action_index, 'batch_count': len(actions)}
        if row is not None:
            # SENDING/UNKNOWN can only be resolved through the unique marker;
            # a missing marker is not permission to repeat a POST.
            receipt = self.platform.find(marker, 'create_issue', json.loads(row['payload']))
            if receipt is None:
                raise ValueError('PUBLICATION_UNKNOWN: no receipt; do not resend')
            already_sent = False
            with self.store.transaction():
                current = self.store.db.execute(
                    'SELECT state,receipt FROM outbox WHERE id=?', (op,)).fetchone()
                if current and current['state'] == 'SENT':
                    receipt = json.loads(current['receipt'])
                    already_sent = True
                    resources = None
                else:
                    self.store.guard(eid, token)
                    self.store.db.execute(
                        "UPDATE outbox SET state='SENT',receipt=? WHERE id=?",
                        (dump(receipt), op))
                    self.store.event('publication.sent', {'operation': op, 'action': 'create_issue'})
                    self.store._renew_locked(eid, self.cfg['lease_seconds'])
                    resources = self.store.finalize_locked(eid)
            self._release_after_finalize(eid, token, resources)
            if already_sent:
                receipt = json.loads(self.store.db.execute(
                    'SELECT receipt FROM outbox WHERE id=?', (op,)).fetchone()['receipt'])
            return {'op': op, 'receipt': receipt, 'push_required': False,
                    'batch_index': action_index, 'batch_count': len(actions)}

        # A new remote write requires the same fresh-snapshot guard as the
        # scalar publication path. Existing outbox rows above are reconciled
        # first so a stale snapshot cannot prevent recovery of a known effect.
        self.sync()
        execution, item, source = self.execution(eid, token)
        current_result = json.loads(execution['result'])
        if dump(current_result) != dump(result):
            raise ValueError('REPORT_CHANGED: retry with the current report')
        self.gate(execution, item, source, result, context)
        payload = dict(entry['payload'])
        payload['body'] = (
            '> 🤖 本条由 code-loop **discover** 循环自动生成\n\n'
            + payload['body'] + '\n\n' + marker
            + '\n<!-- code-loop-agent: discover -->')
        validator = getattr(self.platform, 'validate_action', None)
        if validator is not None:
            validator('create_issue', payload)
        with self.store.transaction():
            self.store.guard(eid, token)
            current = self.store.db.execute(
                'SELECT result FROM executions WHERE id=?', (eid,)).fetchone()
            if not current or dump(json.loads(current['result'] or '{}')) != dump(result):
                raise ValueError('REPORT_CHANGED: retry with the current report')
            if self.store.db.execute('SELECT 1 FROM outbox WHERE id=?', (op,)).fetchone():
                raise ValueError('PUBLICATION_IN_PROGRESS: retry to reconcile')
            self.store.db.execute(
                'INSERT INTO outbox VALUES (?,?,?,?,?,NULL)',
                (op, eid, 'create_issue', dump(payload), 'SENDING'))
            self.store._renew_locked(eid, self.cfg['lease_seconds'])
        return {'op': op, 'receipt': None, 'push_required': False,
                'action': 'create_issue', 'payload': payload,
                'batch_index': action_index, 'batch_count': len(actions)}

    def complete_publish(self, eid, token, op, pushed=None):
        """Second half of publication: the recorded intent is now executed once
        against the platform, or resolved through marker queries only. A SENT
        outbox with all facts in place derives the terminal state (D2) —
        publish is the round's last step, not a precursor to finish."""
        row = self.store.db.execute('SELECT * FROM outbox WHERE id=?', (op,)).fetchone()
        if row is None:
            raise ValueError('PUBLICATION_NOT_PREPARED')
        if row['state'] == 'SENT':
            self.store.derive_terminal(eid)  # idempotent crash-recovery path
            return json.loads(row['receipt'])
        if row['state'] == 'UNKNOWN':
            # Never resend after ambiguous completion. Query is safe even if another sender is alive.
            receipt = self.platform.find('<!-- code-loop:' + op + ' -->', row['action'],
                                         json.loads(row['payload']))
            if receipt is None:
                raise ValueError('PUBLICATION_UNKNOWN: no receipt; do not resend')
            with self.store.transaction():
                self.store.guard(eid, token)
                self.store.db.execute("UPDATE outbox SET state='SENT',receipt=? WHERE id=?", (dump(receipt), op))
                self.store._renew_locked(eid, self.cfg['lease_seconds'])
                resources = self.store.finalize_locked(eid)
            self._release_after_finalize(eid, token, resources)
            return receipt
        if pushed is False:
            self.mark_publish_unknown(op)
            raise ValueError('PUBLICATION_UNKNOWN: query receipt before any retry')
        try:
            self.store.guard(eid, token)
            receipt = self.platform.write(row['action'], json.loads(row['payload']))
        except Exception:
            self.mark_publish_unknown(op)
            raise ValueError('PUBLICATION_UNKNOWN: query receipt before any retry') from None
        try:
            already_sent = False
            with self.store.transaction():
                current = self.store.db.execute('SELECT state,receipt FROM outbox WHERE id=?', (op,)).fetchone()
                if current and current['state'] == 'SENT':
                    # A concurrent reconciler may have committed the same
                    # marker while this sender was receiving its HTTP response.
                    receipt = json.loads(current['receipt'])
                    already_sent = True
                    resources = None
                else:
                    self.store.guard(eid, token)
                    self.store.db.execute("UPDATE outbox SET state='SENT',receipt=? WHERE id=?", (dump(receipt), op))
                    self.store.event('publication.sent', {'operation': op, 'action': row['action']})
                    self.store._renew_locked(eid, self.cfg['lease_seconds'])
                    resources = self.store.finalize_locked(eid)
        except ValueError:
            # The write landed but the lease was stolen before the commit: the
            # outcome is ambiguous, exactly like a lost response — never
            # resend, resolve through the operator's receipt query.
            self.mark_publish_unknown(op)
            raise ValueError('PUBLICATION_UNKNOWN: query receipt before any retry') from None
        if already_sent:
            return receipt
        self._release_after_finalize(eid, token, resources)
        return receipt

    def _release_after_finalize(self, eid, token, resources):
        if resources is not None:
            self.store._release_locks(resources, eid, token)

    def mark_publish_unknown(self, op):
        self.store.db.execute("UPDATE outbox SET state='UNKNOWN' WHERE id=? AND state!='SENT'", (op,))

    def push(self, e, source):
        context = validate_worktree(self.cfg, e['worktree'], e['branch'])
        if not context['branch'] or context['head'] != e['head']:
            raise ValueError('PUSH_CONTEXT_CHANGED')
        # Conflict repair may run on a temp branch; the push always lands on
        # the original PR branch (target_branch resolved in prepare_publish).
        # Explicit refspec; ordinary non-forced push rejects remote divergence.
        target = source.get('target_branch') or context['branch']
        git(e['worktree'], 'push', 'origin', context['head'] + ':refs/heads/' + target)

    def items(self, kind=None):
        return self.store.items(kind)

    def reconcile(self, operation, receipt):
        """Operator action: record a verified remote receipt for an outbox row."""
        self.enabled()
        return self.store.reconcile(operation, receipt)

    def status(self):
        marker = Path(self.cfg['state_dir']) / 'machine-enabled.json'
        # Remote (registry-authorized) services report enabled from the
        # authorization itself; the marker is the legacy single-machine path.
        enabled = self._authorization is not None or marker.is_file()
        return dict(self.store.status(), enabled=enabled, synced_at=self.meta('synced_at'),
                    sync_error=self.meta('sync_error'), state_dir=self.cfg['state_dir'],
                    env_failures=self.meta('env_failures') or {},
                    verification_source='repo' if self._repo_exam else 'machine')
