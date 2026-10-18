"""The capability projection (worker → serve, display-only) and the spawn
options composition.

A machine's own CLI model lists can only be measured on that machine; this
module is how the measured fact travels: the worker reports `{id, models}`
over the command channel, the dispatcher stores a sanitized, connection-
scoped projection, and `spawn_options` composes the dialog payload through
one path for local and remote projects alike. What deliberately does NOT
live here: any second detection implementation (the registry's is the only
one), any authorization (the executor's pre-claim re-check stays the sole
gate), and any persisted state (the projection dies with the connection).
"""
from __future__ import annotations

import time

from .. import agent_runners

# Report caps: a capability report is display data — big enough for any real
# registry, small enough that a broken peer cannot bloat the projection.
MAX_REPORTED_AGENTS = 32
MAX_MODELS_PER_AGENT = 128


def spawn_options(project=None, dispatcher=None, options_fn=None,
                  cards_fn=None):
    """The GET /api/spawn/options payload — the single composition path for
    local and remote projects alike: the executing machine's live capability
    projection when one exists, otherwise the serve host's own detection (an
    honestly labeled fallback). With a fresh projection the detection
    callback is never called — a dropdown request must not cost a CLI start,
    and no "local direct detection" branch may grow beside this path."""
    agents = None
    scope = {'kind': 'serve-host'}
    if project and dispatcher is not None:
        cap = dispatcher.capability_for(project)
        if cap is not None:
            agents = _compose_cards((cards_fn or agent_runners.cards)(),
                                    cap['models'])
            scope = {'kind': 'machine', 'machine': cap['machine'],
                     'reported_at': cap['ts']}
    if agents is None:
        agents = (options_fn or agent_runners.options)().get('agents', [])
    return {'ok': True, 'agents': agents, 'scope': scope}


def _compose_cards(cards, models_by_id):
    """The registry's own cards with the machine's model lists filled in. A
    picker-less plugin stays null (it has no listing command anywhere); a
    modeled plugin missing from the report is empty, not invented — the
    machine did not claim it, so the dialog must say "not installed or
    failed" rather than show the serve host's list."""
    composed = []
    for card in cards:
        row = dict(card)
        if row.get('models') is not None:
            row['models'] = models_by_id.get(row.get('id'), [])
        composed.append(row)
    return composed


class CapabilityProjection:
    """Mixin for ``CommandDispatcher``: stores and reads the per-machine
    capability projection. Uses the dispatcher's ``_lock``/``_connections``/
    ``_by_project``/``_machines`` — one lock, one truth, same lifetime."""

    def capabilities(self, machine, agents):
        """A later ``agent_options`` frame: same store, last write wins — the
        machine's current truth, not an addendum."""
        self._store_capabilities(machine['id'], agents)

    def _store_capabilities(self, machine_id, agents):
        """Sanitize a capability report into the display projection. Agent
        ids outside the registry's whitelist are dropped, model tokens are
        charset-checked and deduped, and the report is size-capped — a
        forged or broken report can at worst draw an empty picker, never a
        card the registry does not know. Only ``{id: models}`` survives:
        labels and captions are composed from THIS server's registry, so a
        reported string never enters the display plane. The projection is
        display data, not authorization — the executor re-checks a chosen
        model against its own list before any spawn."""
        if not isinstance(agents, list):
            return
        allowed = set(agent_runners.ids())
        models = {}
        for row in agents[:MAX_REPORTED_AGENTS]:
            if not isinstance(row, dict):
                continue
            agent_id = row.get('id')
            # isinstance first: an unhashable id would crash set membership
            # and take the whole WS channel down — the one thing a foreign
            # frame must never do (a broken report is an empty picker, never
            # a dead connection).
            if not isinstance(agent_id, str) or agent_id not in allowed:
                continue
            reported = row.get('models')
            if reported is None:
                models[agent_id] = None   # no picker on that machine
            elif isinstance(reported, list):
                seen = []
                for token in reported:
                    if len(seen) >= MAX_MODELS_PER_AGENT:
                        break
                    if agent_runners.valid_model_id(token) and token not in seen:
                        seen.append(token)
                models[agent_id] = seen
        with self._lock:
            self._capabilities[machine_id] = {'models': models,
                                              'ts': time.time()}

    def capability_for(self, key):
        """The live capability projection for a project's executing machine,
        or None when there is no unambiguous one: no machine, several (the
        one-project-one-machine constraint broken), no report, or a stale one
        (older than two detection TTLs — the pump refreshes at one, so its
        death must age the projection out rather than serve a fossil). The
        consumer degrades to the serve host's own detection, honestly
        labeled."""
        with self._lock:
            mids = sorted(mid for mid in self._by_project.get(key, ())
                          if mid in self._connections)
            if len(mids) != 1:
                return None
            cap = self._capabilities.get(mids[0])
            if cap is None:
                return None
            if time.time() - cap['ts'] > 2 * agent_runners.CACHE_TTL_SECONDS:
                return None
            machine = self._machines.get(mids[0]) or {}
            return {'models': dict(cap['models']), 'ts': cap['ts'],
                    'machine': machine.get('name') or mids[0]}
