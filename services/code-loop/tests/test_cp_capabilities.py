#!/usr/bin/env python3
"""The capability projection end to end: worker reports sanitized into a
connection-scoped display projection (dispatcher side) and the single-path
spawn options composition (serve side). The invariant pinned here: a fresh
projection must cost ZERO detection calls -- no "local direct detection"
branch may grow beside the one composition path."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop import agent_runners
from codeloop.cp import server
from codeloop.cp.capabilities import spawn_options
from codeloop.cp.server import Coordinator


def cards():
    """The registry's own card identities (models: null = no picker)."""
    return [
        {'id': 'claude', 'label': 'claude', 'sub': 'Claude Code',
         'models': None},
        {'id': 'opencode', 'label': 'opencode', 'sub': 'open agent',
         'models': []},
        {'id': 'pi', 'label': 'pi', 'sub': 'pi coding agent', 'models': []},
    ]


def no_detection():
    raise AssertionError('detection must not run when a live projection exists')


class FakeDispatcher:
    def __init__(self, cap):
        self.cap = cap
        self.asked = []

    def capability_for(self, project):
        self.asked.append(project)
        return self.cap


CAP = {'models': {'opencode': ['x/y'], 'pi': ['selfhost/ds']},
       'machine': 'GPU-02', 'ts': 1789960000.0}


class FreshProjectionTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = FakeDispatcher(CAP)

    def payload(self):
        return spawn_options(project='a__b', dispatcher=self.dispatcher,
                             options_fn=no_detection, cards_fn=cards)

    def test_scope_names_the_executing_machine_and_its_report_time(self):
        payload = self.payload()
        self.assertEqual({'kind': 'machine', 'machine': 'GPU-02',
                          'reported_at': CAP['ts']}, payload['scope'])

    def test_models_come_from_the_projection_detection_never_runs(self):
        payload = self.payload()
        self.assertEqual([c['id'] for c in cards()],
                         [a['id'] for a in payload['agents']])
        models = {a['id']: a['models'] for a in payload['agents']}
        self.assertEqual(['x/y'], models['opencode'])
        self.assertEqual(['selfhost/ds'], models['pi'])

    def test_pickerless_card_stays_null_not_empty(self):
        payload = self.payload()
        claude = next(a for a in payload['agents'] if a['id'] == 'claude')
        self.assertIsNone(claude['models'])

    def test_modeled_agent_missing_from_the_report_is_empty(self):
        self.dispatcher.cap = {'models': {'opencode': ['x/y']},
                               'machine': 'GPU-02', 'ts': 1.0}
        payload = self.payload()
        pi = next(a for a in payload['agents'] if a['id'] == 'pi')
        self.assertEqual([], pi['models'])


class FallbackTests(unittest.TestCase):
    def calls(self):
        calls = []

        def options_fn():
            calls.append(1)
            return {'ok': True, 'agents': [
                {'id': 'opencode', 'label': 'opencode', 'sub': 'open agent',
                 'models': ['serve-host/model']}]}

        return options_fn, calls

    def test_no_projection_falls_back_to_serve_host_detection(self):
        options_fn, calls = self.calls()
        payload = spawn_options(project='a__b',
                                dispatcher=FakeDispatcher(None),
                                options_fn=options_fn, cards_fn=cards)
        self.assertEqual({'kind': 'serve-host'}, payload['scope'])
        self.assertEqual(['serve-host/model'],
                         payload['agents'][0]['models'])
        self.assertEqual(1, len(calls))

    def test_no_project_means_the_legacy_payload_with_a_scope(self):
        options_fn, calls = self.calls()
        dispatcher = FakeDispatcher(CAP)
        payload = spawn_options(project='', dispatcher=dispatcher,
                                options_fn=options_fn, cards_fn=cards)
        self.assertEqual({'kind': 'serve-host'}, payload['scope'])
        self.assertEqual([], dispatcher.asked)   # no project: nothing to look up
        self.assertEqual(1, len(calls))

    def test_a_dispatcherless_serve_degrades_to_detection(self):
        options_fn, calls = self.calls()
        payload = spawn_options(project='a__b', dispatcher=None,
                                options_fn=options_fn, cards_fn=cards)
        self.assertEqual({'kind': 'serve-host'}, payload['scope'])
        self.assertEqual(1, len(calls))



CAP_PROJECT_DOC = (
    'The capability projection: a machine\'s reported model lists, keyed by '
    'its authenticated connection, sanitized into display-only data and '
    'dropped the moment the connection drops. The dropdown\'s facts live '
    'exactly as long as the machine that reported them; sanitization keeps a '
    'reported string out of the display plane (ids must be '
    'registry-whitelisted, model tokens charset-checked) and the '
    'executor\'s own pre-claim re-check stays the only authorization.')


class FakeConnection:
    """A scripted WS agent: send_json records, send failure raises."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.closed = False

    def send_json(self, payload):
        if self.fail:
            raise OSError('socket gone')
        self.sent.append(payload)

    def close(self):
        self.closed = True


class CapabilityProjectionTests(unittest.TestCase):
    """See CAP_PROJECT_DOC above (kept out of the class to stay short)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.root = Path(self.tmp.name)
        cfg_dir = self.root / 'projects' / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
             'state_dir': str(cfg_dir / 'state'),
             'project_key': 'a__b', 'snapshot_ttl': 3600}), encoding='utf-8')
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')
        self.registry = self.coordinator.registry()
        code = self.registry.create_enroll_code('agent-box', ['a__b'])
        out = self.registry.enroll(code, '1', '127.0.0.1')
        self.machine = self.registry.authenticate(out['token'])
        self.dispatcher = self.coordinator.dispatcher
        self.dispatcher.register(self.machine, FakeConnection())
        self.dispatcher.hello(self.machine, ['a__b'])

    def tearDown(self):
        self.registry.close()
        import gc
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def report(self, agents, machine=None):
        self.dispatcher.capabilities(machine or self.machine, agents)

    def test_report_is_sanitized_into_display_only_data(self):
        self.report([
            {'id': 'pi', 'models': ['selfhost/ds', 'BAD TOKEN', 7,
                                    'selfhost/ds']},
            {'id': 'codex', 'models': ['x/y']},
            {'id': 'claude', 'models': None},
            'junk', {'models': ['a/b']},
        ])
        cap = self.dispatcher.capability_for('a__b')
        self.assertEqual('agent-box', cap['machine'])
        self.assertEqual({'pi': ['selfhost/ds'], 'claude': None}, cap['models'])
        self.assertIsInstance(cap['ts'], float)

    def test_unhashable_or_wrongtype_ids_never_crash_the_channel(self):
        """A foreign frame must never take the WS channel down: the sanitizer
        skips anything it cannot even test for whitelist membership."""
        self.report([
            {'id': ['pi'], 'models': ['a/b']},
            {'id': {'k': 1}, 'models': ['a/b']},
            {'id': None, 'models': ['a/b']},
            {'id': 'pi', 'models': 'not-a-list'},
        ])
        self.assertEqual({}, self.dispatcher.capability_for('a__b')['models'])

    def test_report_is_size_capped(self):
        self.report([{'id': 'pi', 'models': ['a/%d' % i for i in range(300)]}])
        models = self.dispatcher.capability_for('a__b')['models']['pi']
        self.assertEqual(128, len(models))

    def test_agent_options_frame_is_last_write_wins(self):
        self.report([{'id': 'pi', 'models': ['a/b']}])
        self.report([{'id': 'pi', 'models': ['c/d']}])
        self.assertEqual(['c/d'],
                         self.dispatcher.capability_for('a__b')['models']['pi'])

    def test_unregister_drops_the_projection(self):
        self.report([{'id': 'pi', 'models': ['a/b']}])
        self.dispatcher.unregister(self.machine['id'])
        self.assertIsNone(self.dispatcher.capability_for('a__b'))

    def test_capability_needs_a_single_live_reported_machine(self):
        self.assertIsNone(self.dispatcher.capability_for('a__b'))   # no report
        self.report([{'id': 'pi', 'models': ['a/b']}])
        self.dispatcher.unregister(self.machine['id'])
        self.dispatcher.register(self.machine, FakeConnection())
        self.dispatcher.hello(self.machine, ['a__b'])
        self.assertIsNone(self.dispatcher.capability_for('a__b'))   # report gone
        code = self.registry.create_enroll_code('late-box', ['a__b'])
        out = self.registry.enroll(code, '2', '127.0.0.1')
        machine2 = self.registry.authenticate(out['token'])
        self.dispatcher.register(machine2, FakeConnection())
        self.dispatcher.hello(machine2, ['a__b'])
        self.dispatcher.capabilities(machine2, [{'id': 'pi', 'models': ['e/f']}])
        self.assertIsNone(self.dispatcher.capability_for('a__b'))   # ambiguous

    def test_stale_projection_expires(self):
        self.report([{'id': 'pi', 'models': ['a/b']}])
        self.assertIsNotNone(self.dispatcher.capability_for('a__b'))
        future = time.time() + 2 * agent_runners.CACHE_TTL_SECONDS + 1
        with mock.patch.object(server.time, 'time', return_value=future):
            self.assertIsNone(self.dispatcher.capability_for('a__b'))

    def test_legacy_hello_without_agents_stays_unprojected(self):
        d = self.coordinator.dispatcher
        d.hello(self.machine, ['a__b'])
        self.assertIsNone(d.capability_for('a__b'))

    def test_hello_can_carry_the_first_report(self):
        d = self.coordinator.dispatcher
        d.hello(self.machine, ['a__b'],
                agents=[{'id': 'pi', 'models': ['selfhost/ds']}])
        self.assertEqual({'pi': ['selfhost/ds']},
                         d.capability_for('a__b')['models'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
