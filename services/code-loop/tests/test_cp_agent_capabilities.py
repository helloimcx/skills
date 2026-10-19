#!/usr/bin/env python3
"""The worker side of the capability projection: the detection pump and the
hello announcement. Detection runs OFF the frame loop (a listing command
costs a CLI start); a failed detection is skipped -- the pump must never
die, or the server-side projection silently ages out."""
import queue
import sys
import threading
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop import agent as agent_mod




CAP_PUMP_DOC = (
    'The capability pump: detection runs OFF the frame loop (a listing '
    'command costs a CLI start), hands ready agent_options frames to the '
    'channel queue, and hello announces the last-known snapshot so a '
    'reconnecting agent projects immediately. A failed detection is skipped '
    '-- the pump must never die, or the projection silently ages out '
    'server-side.')


class CapabilityPumpTests(unittest.TestCase):
    """See the module docstring."""

    def make(self, options_fn, refresh=0.01):
        return agent_mod.WorkerAgent(
            server_url='http://cp.example', token='tok', client=object(),
            capability_refresh_seconds=refresh,
            capability_options_fn=options_fn)

    def test_capability_frame_is_id_and_models_only(self):
        frame = agent_mod.capability_frame(lambda: {'agents': [
            {'id': 'pi', 'label': 'pi', 'sub': 'pi coding agent',
             'models': ['selfhost/ds']},
            {'id': 'claude', 'models': None}]})
        self.assertEqual({'type': 'agent_options', 'agents': [
            {'id': 'pi', 'models': ['selfhost/ds']},
            {'id': 'claude', 'models': None}]}, frame)
        self.assertNotIn('label', frame['agents'][0])   # ours, never reported

    def test_pump_delivers_and_hello_announces_last_known(self):
        agent = self.make(lambda: {'agents': [{'id': 'pi',
                                               'models': ['selfhost/ds']}]})
        self.assertNotIn('agents', agent.hello_frame())   # nothing computed yet
        commands, stop = queue.Queue(), threading.Event()
        pump = threading.Thread(target=agent._capability_pump,
                                args=(commands, stop), daemon=True)
        pump.start()
        item = commands.get(timeout=2.0)
        stop.set()
        self.assertIsInstance(item, agent_mod._CapabilityFrame)
        self.assertEqual([{'id': 'pi', 'models': ['selfhost/ds']}],
                         item.frame['agents'])
        self.assertEqual([{'id': 'pi', 'models': ['selfhost/ds']}],
                         agent.hello_frame()['agents'])

    def test_pump_survives_detection_failure(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('cli hung')
            return {'agents': [{'id': 'pi', 'models': ['selfhost/ds']}]}

        agent = self.make(flaky)
        commands, stop = queue.Queue(), threading.Event()
        pump = threading.Thread(target=agent._capability_pump,
                                args=(commands, stop), daemon=True)
        pump.start()
        item = commands.get(timeout=2.0)
        stop.set()
        self.assertEqual([{'id': 'pi', 'models': ['selfhost/ds']}],
                         item.frame['agents'])
        self.assertEqual(2, len(calls))

if __name__ == '__main__':
    unittest.main(verbosity=2)
