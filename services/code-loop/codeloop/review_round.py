"""Shared reviewer-round preparation: claim -> worktree -> bind.

One round, one carrier — the worker agent's command dispatch, which the local
machine runs identically over loopback: ``RemoteCoordinator`` rides a
machine's ``WorkerService`` (HTTP + policy cache; no abandon — a claim this
machine cannot hand off simply lapses with its short lease). Everything after
the claim is deterministic and machine-local: the PR facts come from the
claimed item row itself, the worktree is cut detached at that very head so the
bind guard can only pass on this commit, and the bind hands the prepared round
to a session that adopts it. Any failure after the claim removes the
half-made worktree — a round that cannot start must never cost a session.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .cp import runtime as cp_runtime

# The click→bind handoff window of a pre-claimed round: the session's bind
# renews the lease to the configured one, and a claim nobody adopts stops
# blocking the item when this window lapses.
PRECLAIM_TTL_SECONDS = 300

# Which work item a round claims, by loop: the reviewer round targets the PR's
# review item, the repair round (dashboard conflict fix) the handler's repair
# item. A loop outside this map cannot carry a prepared round — refuse it
# loudly instead of claiming some unintended item.
ITEM_KEY = {'reviewer': 'review:%d', 'handler': 'repair:%d'}


class PrepareError(Exception):
    """A round that cannot be prepared, refusing with an HTTP-shaped status
    and an operator-readable message (the UI surfaces it verbatim)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def cp_config(row, collect_root, org_repo):
    """Resolve the project's machine-local control-plane config against the
    registered repo. Loud on any failure — a round that cannot reach the
    control plane must never become a session that discovers that on its own."""
    path = (Path(row['config_path']) if row['config_path']
            else Path(collect_root) / org_repo / 'control-plane.json')
    if not path.is_file():
        raise PrepareError(503, 'REGISTER_INVALID: control-plane.json: %s' % path)
    try:
        return cp_runtime.load_config(str(path), cwd=row['repo_cwd'])
    except Exception as exc:
        raise PrepareError(503, 'REGISTER_INVALID: %s: %s'
                           % (type(exc).__name__, exc)) from None


def _claim_refused(item_key, exc):
    """Translate a claim rejection into the executor's vocabulary. Domain
    rejections (ITEM_UNKNOWN / MACHINE_NOT_ENABLED / …) arrive verbatim from
    the server — the envelope message and an in-process ValueError share the
    leading-CODE shape."""
    message = str(exc)
    if message.startswith('ITEM_UNKNOWN'):
        return PrepareError(409, 'CLAIM_UNAVAILABLE: %s 不存在（未派生 / 已完成 / '
                                 '处于修复流程）' % item_key)
    if message.startswith('MACHINE_NOT_ENABLED'):
        return PrepareError(409, 'MACHINE_NOT_ENABLED')
    return PrepareError(409, 'CLAIM_UNAVAILABLE: %s' % message)


def _claim_unavailable(item_key, blocked_by):
    """Honest refusal wording (issue #254): a directed claim that lost to a
    resource conflict names the blocker and its unlock time instead of the
    generic three-way guess. ``blocked_by`` is the store's structured summary
    (item key, worker, state, conflicting resources, expiry, stealable)."""
    if not blocked_by:
        return 'CLAIM_UNAVAILABLE: %s 当前不可领取（进行中 / 已完成 / 评审并发已满）' % item_key
    parts = []
    for b in blocked_by:
        when = '已过期可窃取' if b.get('stealable') else '租约至 %s' % time.strftime(
            '%H:%M', time.localtime(b.get('expires_at', 0)))
        res = ','.join(b.get('resources') or [])
        parts.append('%s（%s，%s，持 %s，%s）'
                     % (b.get('item_key'), b.get('worker'), b.get('state'), res, when))
    return 'CLAIM_UNAVAILABLE: %s 被 %s 阻塞' % (item_key, '；'.join(parts))


class RemoteCoordinator:
    """The one execution carrier: a worker machine's ``WorkerService`` — the
    local machine runs it too (loopback), which is what "no local/remote
    distinction" means in practice. The claim rides HTTP (server composes
    <machine>/<loop>; policy lands in the machine cache), the bind validates
    the worktree locally, posts the context and records it for the adopting
    session's later verify/publish. There is deliberately no abandon surface
    on this side — an unadopted claim expires with its lease through the
    standard sweep."""

    def __init__(self, worker, loop='reviewer'):
        self.worker = worker
        self.loop = loop
        self._item = None

    def claim(self, number, ttl=PRECLAIM_TTL_SECONDS):
        key = ITEM_KEY.get(self.loop)
        if key is None:
            raise PrepareError(400, 'INVALID_LOOP: %r cannot carry a prepared '
                                    'round' % (self.loop,))
        item_key = key % number
        try:
            result = self.worker.claim_raw(self.loop, item_id=item_key, ttl=ttl)
        except ValueError as exc:
            # Transport/protocol failures (CP_SERVER_*) mean "could not ask",
            # which is not the same as "the control plane refused": they are
            # 502 retryables, never a 409 unavailability.
            if str(exc).startswith('CP_SERVER_'):
                raise PrepareError(502, 'CLAIM_FAILED: %s' % str(exc)) from None
            raise _claim_refused(item_key, exc) from None
        except Exception as exc:
            raise PrepareError(502, 'CLAIM_FAILED: %s: %s'
                               % (type(exc).__name__, exc)) from None
        execution = result.get('execution')
        if execution is None:
            raise PrepareError(409, _claim_unavailable(item_key, result.get('blocked_by')))
        self._item = result.get('item')
        return execution

    def pr_facts(self, claim):
        item = self._item if (self._item or {}).get('id') == claim.get('item_id') else None
        if item is None:
            raise ValueError('CLAIMED_ITEM_MISSING: %s' % claim.get('item_id'))
        return json.loads(item['payload'])['pr']

    def bind(self, claim, worktree, branch=None):
        # WorkerService.bind validates the worktree against this machine's
        # config, posts the context and records it — the adopting session's
        # execution() reads exactly that record. A repair round passes the
        # branch it had checked out (handler bind guards on it); a review
        # round stays detached (branch=None).
        self.worker.bind(claim['id'], claim['token'], worktree, branch)

    def abandon_quiet(self, execution, token):
        pass   # no worker-side abandon: the short lease self-heals


def remove_worktree_quietly(cp_cfg, path, git_fn=None):
    """Best effort: the original failure owns the error message."""
    if not path or not cp_cfg:
        return
    git = git_fn or cp_runtime.git
    try:
        git(cp_cfg['repo_root'], 'worktree', 'remove', str(path))
    except Exception:
        pass


def _reclaim_leftover_worktree(cp_cfg, path, git):
    """Remove a dead round's leftover at this feature's own naming so a retry
    is not blocked by our own debris. Safe because the caller already holds
    the claim: no live execution owns this PR's item, so nothing legitimate
    can be working in it. git itself is the guard — ``worktree remove``
    without --force refuses a dirty worktree and a path that is not a
    registered worktree of this repository; anything it refuses is an
    operator's to clean, never ours to delete."""
    try:
        git(cp_cfg['repo_root'], 'worktree', 'remove', str(path))
        return True
    except Exception:
        return False


def _prepare_worktree(cp_cfg, number, pr, git_fn=None):
    """Create this round's review worktree at the PR head, detached. The head
    comes from the claimed item, so the bind guard (worktree head ==
    payload.pr.head.sha) can only pass on this very commit."""
    git = git_fn or cp_runtime.git
    head = pr['head']['sha']
    path = Path(cp_cfg['worktree_root']) / ('review-pr-%d' % number)
    if path.exists() and not _reclaim_leftover_worktree(cp_cfg, path, git):
        raise PrepareError(502, 'WORKTREE_FAILED: 目录已存在且不可回收（非本仓库注册的干净 '
                                'worktree，需人工清理）：%s' % path)
    try:
        try:
            git(cp_cfg['repo_root'], 'cat-file', '-e', head + '^{commit}')
        except ValueError:
            git(cp_cfg['repo_root'], 'fetch', 'origin', pr['head']['ref'])
        git(cp_cfg['repo_root'], 'worktree', 'add', '--detach', str(path), head)
        actual = git(str(path), 'rev-parse', 'HEAD')
    except Exception as exc:
        # git does not only fail with GIT_FAILED: a timeout arrives as
        # subprocess.SubprocessError and a missing binary as OSError. Nothing
        # may escape past this point — the claim must be handed back and a
        # half-made worktree must not linger.
        remove_worktree_quietly(cp_cfg, path, git)
        raise PrepareError(502, 'WORKTREE_FAILED: %s: %s'
                           % (type(exc).__name__, exc)) from None
    if actual != head:
        remove_worktree_quietly(cp_cfg, path, git)
        raise PrepareError(502, 'WORKTREE_FAILED: HEAD %s != PR head %s' % (actual, head))
    return str(path)


def _repair_temp_branch(cp_cfg, number):
    """The temp repair branch name for a conflict round: the project's fix
    prefix (repo convention: bug-type work rides fix/) + repair-pr-<n> — a
    namespace this feature owns, so a leftover from a dead round is ours to
    reuse (``-B`` moves it back onto the head)."""
    prefixes = cp_cfg.get('branch_prefixes') or ['fix/']
    prefix = next((p for p in prefixes if p.startswith('fix')), prefixes[0])
    return prefix + 'repair-pr-%d' % number


def _branch_occupied(cp_cfg, branch, git):
    """True when some worktree of this repository already has the branch
    checked out — git allows one worktree per branch."""
    out = git(cp_cfg['repo_root'], 'worktree', 'list', '--porcelain')
    marker = 'refs/heads/' + branch
    return any(line.startswith('branch ') and line[7:] == marker
               for line in out.splitlines())


def _local_branch_tip(cp_cfg, branch, git):
    """The local branch's commit, or None when the branch does not exist
    locally (a PR branch often exists only on the platform)."""
    try:
        return git(cp_cfg['repo_root'], 'rev-parse', '--verify', '--quiet',
                   'refs/heads/' + branch) or None
    except ValueError:
        return None


def _branch_allowed(cp_cfg, branch):
    """Shape A must survive validate_worktree's own branch rule: the checked
    out branch has to hit the project's prefix list and never be the base.
    A PR head branch outside that rule cannot be checked out for a round at
    all — pick the temp branch instead of binding into a guaranteed refusal."""
    if branch == cp_cfg.get('base'):
        return False
    return any(branch.startswith(p) for p in (cp_cfg.get('branch_prefixes') or []))


def _prepare_repair_worktree(cp_cfg, number, pr, git_fn=None):
    """Conflict-resolution worktree: a *branch* checkout the session can push
    to. The original PR branch when it is free, allowed by the project's
    branch convention, and exactly at the platform head; otherwise the temp
    repair branch starting at the head SHA — the two shapes the control
    plane's bind guard accepts for PR_CONFLICT_RESOLUTION. A local branch
    that drifted from the head is never moved silently: the temp branch is
    cut from the head instead, so the round can never merge from anything
    less than the reviewed commit."""
    git = git_fn or cp_runtime.git
    head = pr['head']['sha']
    ref = pr['head']['ref']
    path = Path(cp_cfg['worktree_root']) / ('repair-pr-%d' % number)
    if path.exists() and not _reclaim_leftover_worktree(cp_cfg, path, git):
        raise PrepareError(502, 'WORKTREE_FAILED: 目录已存在且不可回收（非本仓库注册的干净 '
                                'worktree，需人工清理）：%s' % path)
    branch = None
    try:
        try:
            git(cp_cfg['repo_root'], 'cat-file', '-e', head + '^{commit}')
        except ValueError:
            git(cp_cfg['repo_root'], 'fetch', 'origin', ref)
        if _branch_allowed(cp_cfg, ref) \
                and _local_branch_tip(cp_cfg, ref, git) == head \
                and not _branch_occupied(cp_cfg, ref, git):
            branch = ref
            git(cp_cfg['repo_root'], 'worktree', 'add', str(path), ref)
        else:
            branch = _repair_temp_branch(cp_cfg, number)
            git(cp_cfg['repo_root'], 'worktree', 'add', '-B', branch,
                str(path), head)
        actual = git(str(path), 'rev-parse', 'HEAD')
    except Exception as exc:
        # same bargain as the review worktree: nothing escapes past this
        # point with the claim held and a half-made worktree left behind
        remove_worktree_quietly(cp_cfg, path, git)
        raise PrepareError(502, 'WORKTREE_FAILED: %s: %s'
                           % (type(exc).__name__, exc)) from None
    if actual != head:
        remove_worktree_quietly(cp_cfg, path, git)
        raise PrepareError(502, 'WORKTREE_FAILED: HEAD %s != PR head %s' % (actual, head))
    return branch, str(path)


def prepare_round(coordinator, cp_cfg, number, git_fn=None,
                  ttl=PRECLAIM_TTL_SECONDS):
    """Claim the target work item, cut and bind its worktree, and return the
    round a session can adopt: ``{'execution', 'token', 'worktree', 'branch'}``
    (branch is None for the detached review worktree, the checked-out branch
    for a repair round). A claim the control plane refuses (or any later
    failure) raises ``PrepareError`` with the precise reason — no session is
    ever spent on a round that cannot start, and every post-claim failure
    hands the claim back (and removes the half-made worktree) before
    propagating."""
    claim = coordinator.claim(number, ttl=ttl)
    repair = getattr(coordinator, 'loop', 'reviewer') == 'handler'
    try:
        pr = coordinator.pr_facts(claim)
    except Exception as exc:
        coordinator.abandon_quiet(claim['id'], claim['token'])
        raise PrepareError(502, 'CLAIM_FAILED: %s: %s'
                           % (type(exc).__name__, exc)) from None
    try:
        if repair:
            branch, worktree = _prepare_repair_worktree(cp_cfg, number, pr,
                                                        git_fn)
        else:
            branch, worktree = None, _prepare_worktree(cp_cfg, number, pr,
                                                       git_fn)
    except PrepareError:
        coordinator.abandon_quiet(claim['id'], claim['token'])
        raise
    try:
        coordinator.bind(claim, worktree, branch)
    except Exception as exc:
        coordinator.abandon_quiet(claim['id'], claim['token'])
        remove_worktree_quietly(cp_cfg, worktree, git_fn)
        raise PrepareError(502, 'BIND_FAILED: %s: %s'
                           % (type(exc).__name__, exc)) from None
    return {'execution': claim['id'], 'token': claim['token'],
            'worktree': worktree, 'branch': branch,
            'head_ref': pr['head']['ref'] if repair else None}
