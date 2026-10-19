"""Agent-runner registry: the single source of truth for coding agents.

One fact, three consumers, three trust levels. The registry is imported by
the control plane's command-validation surface (which agents may an operator
click name), by the dashboard's spawn-options endpoint (what the trigger
dialog offers, detected on the serve host), and by the resident worker agent
(what argv a request resolves to, re-checked on the machine that pays for the
session). Adding a coding agent is one plugin module plus one ``register``
line below — no consumer enumerates agents on its own.

Discovery is explicit, not a directory scan: this registry sits in the
fail-closed path of a control plane, and a plugin that failed to import would
otherwise vanish silently, leaving a click that names an agent nobody can
explain. An explicit import either loads or raises at startup.

Import direction: this package depends only on ``codeloop.cli`` and the
standard library. It must never import ``codeloop.trigger`` or ``codeloop.cp``
— the control plane imports *it*, so reaching back would close a cycle.
"""
from __future__ import annotations

import threading
import time

from . import claude, opencode, pi
from .base import (DEFAULT_MODELS_TIMEOUT, MODEL_ID_RE, Runner, detect_models,
                   valid_model_id)

CACHE_TTL_SECONDS = 600.0

__all__ = ['Runner', 'Registry', 'REGISTRY', 'CACHE_TTL_SECONDS', 'cards', 'detect_models',
           'ids', 'options', 'reset_cache', 'session_reader', 'spawn_argv',
           'valid_model_id', 'validated']


class Registry:
    """Ordered plugin table. Insertion order is the dialog's card order, and
    the order the refusal message lists agents in."""

    def __init__(self):
        self._runners = {}
        self._cache = {'ts': None, 'payload': None}
        self._cache_lock = threading.Lock()

    # -- registration --------------------------------------------------------
    def register(self, runner):
        """Add one plugin. A duplicate or empty id is a programming error and
        fails at import time, never silently overriding a plugin."""
        runner_id = getattr(runner, 'id', None)
        if not isinstance(runner_id, str) or not runner_id:
            raise ValueError('RUNNER_INVALID: id must be a non-empty string')
        if runner_id in self._runners:
            raise ValueError('RUNNER_DUPLICATE: %s already registered' % runner_id)
        self._runners[runner_id] = runner
        return runner

    def get(self, runner_id):
        return self._runners.get(runner_id) if isinstance(runner_id, str) else None

    def ids(self):
        return tuple(self._runners)

    def all(self):
        return tuple(self._runners.values())

    # -- operator input ------------------------------------------------------
    def validated(self, agent, model):
        """Return the normalized (agent, model) pair or raise ValueError.

        The single enqueue-side gate: a whitelist of registered ids plus a
        strict model-id charset, so the command payload stays doorbell-pure —
        enough to pick an argv preset in code, never enough to carry content.
        A model without a modeled agent is meaningless (the plugin has no
        picker), so it is refused instead of silently dropped on the executor.
        ``agent=None`` keeps the historical registered-command behavior."""
        if agent is None and model is None:
            return None, None
        if agent is None:
            raise ValueError('INVALID_COMMAND: model requires agent')
        runner = self.get(agent)
        if runner is None:
            raise ValueError('INVALID_COMMAND: agent must be one of '
                             + ','.join(self.ids()))
        if model is None:
            return runner.id, None
        if not runner.has_models:
            raise ValueError('INVALID_COMMAND: model not supported for agent %s'
                             % runner.id)
        if not valid_model_id(model):
            raise ValueError('INVALID_COMMAND: model must match provider/model')
        return runner.id, model

    # -- executor side -------------------------------------------------------
    def spawn_argv(self, agent, model=None, which_fn=None, run_fn=None):
        """The argv preset for a request, or None meaning "use this machine's
        registered spawn command" (no agent chosen). Resolution is the
        executing machine's own call — a plugin re-checks a requested model
        against this machine's list here, before any claim is taken."""
        if agent is None:
            return None
        runner = self.get(agent)
        if runner is None:
            raise ValueError('INVALID_COMMAND: agent must be one of '
                             + ','.join(self.ids()))
        return runner.argv(model, which_fn=which_fn, run_fn=run_fn)

    # -- dashboard side ------------------------------------------------------
    def reset_cache(self):
        with self._cache_lock:
            self._cache['ts'] = None
            self._cache['payload'] = None

    def options(self, now=None, run_fn=None, ttl=CACHE_TTL_SECONDS):
        """The GET /api/spawn/options payload, cached: a listing command costs
        a CLI start, and the dialog opens far more often than models change.
        A detection failure degrades to an empty list, never an error page —
        the dialog still works, just without that plugin's picker."""
        now = time.time() if now is None else now
        with self._cache_lock:
            cached = self._cache
            if cached['payload'] is not None and now - cached['ts'] < ttl:
                return cached['payload']
            payload = {'ok': True, 'agents': [
                {'id': runner.id, 'label': runner.label, 'sub': runner.sub,
                 'models': runner.models(run_fn=run_fn)}
                for runner in self._runners.values()]}
            cached['ts'] = now
            cached['payload'] = payload
            return payload


REGISTRY = Registry()
REGISTRY.register(claude.RUNNER)
REGISTRY.register(opencode.RUNNER)
REGISTRY.register(pi.RUNNER)


def ids():
    return REGISTRY.ids()


def validated(agent, model):
    return REGISTRY.validated(agent, model)


def spawn_argv(agent, model=None, which_fn=None, run_fn=None):
    return REGISTRY.spawn_argv(agent, model, which_fn=which_fn, run_fn=run_fn)


def session_reader(agent=None):
    """The output-face dispatch: which plugin reads an execution's session
    output on the machine that ran it. ``agent=None`` (the default
    registered-command path) reads claude — every machine that registered
    before pluggable agents existed registered claude's spawn command, and
    that must keep reading exactly as before. An unknown id degrades to the
    same default rather than inventing a reader for a name nobody knows."""
    return REGISTRY.get(agent) or REGISTRY.get('claude')


def options(now=None, run_fn=None, ttl=CACHE_TTL_SECONDS):
    return REGISTRY.options(now=now, run_fn=run_fn, ttl=ttl)


def cards():
    """Card identities without detection: id/label/sub come from this
    registry alone (the display plane never trusts a reported string), and a
    picker-less plugin keeps ``models=None``. The model lists arrive
    separately — a live capability projection from the executing machine —
    so composing them never costs a CLI start here."""
    return [{'id': r.id, 'label': r.label, 'sub': r.sub,
             'models': None if not r.has_models else []}
            for r in REGISTRY.all()]


def reset_cache():
    REGISTRY.reset_cache()
