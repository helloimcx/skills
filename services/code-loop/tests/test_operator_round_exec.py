"""Regression: operator_round with a claude-style argv (None = "use this
machine's registered spawn command") must fall back to DEFAULT_COMMAND.
Production catch 2026-10-08: both live rounds FAILED with
"TypeError: 'NoneType' object is not subscriptable" because resolve_command
subscripted the None argv after a successful claim."""
import socket
import sys as _sys
import tempfile
import unittest
from pathlib import Path as _Path
from types import SimpleNamespace
from unittest import mock

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop import trigger
from codeloop.operator import round as round_module
from codeloop.operator import store as op_store


class ExecuteOperatorRoundTests(unittest.TestCase):
    def run_round(self, spawn_argv_result):
        """Drive one operator_round command through the live executor with a
        stubbed spawn; returns (command_row, captured_argv)."""
        home = tempfile.mkdtemp(prefix='op-exec-')
        store = op_store.open_store(home)
        item, _ = op_store.put_signal(store, source='manual', kind='manual',
                                      key='k1', payload={}, now=1.0)
        row = store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                               (item['id'],)).fetchone()
        store.enqueue_command('operator_round', number=row['rowid'], agent='claude')
        pending = store.pending_commands()[0]
        machine = socket.gethostname()
        claimed = store.mark_dispatched(pending['id'], machine)
        captured = {}

        def fake_spawn(cfg, state_dir, prompt, dry_run, **kw):
            captured['argv'] = cfg['spawn']['command']
            return {'pid': None, 'argv': captured['argv'], 'log': None,
                    'proc': None}

        agent = SimpleNamespace(spawn_argv_fn=lambda a, m=None: spawn_argv_result)
        try:
            with mock.patch.object(round_module.op_store, 'open_store',
                                   lambda: store), \
                 mock.patch.object(round_module.trigger, 'spawn_worker',
                                   fake_spawn):
                round_module.execute_operator_round(agent, {
                    'id': claimed['id'], 'action': 'operator_round',
                    'number': claimed['number'], 'item': item['id'],
                    'agent': 'claude'})
        finally:
            store.db.close()
        reader = op_store.open_store(home)
        try:
            cmd = reader.db.execute('SELECT * FROM commands WHERE id=?',
                                    (claimed['id'],)).fetchone()
            return dict(cmd), captured['argv']
        finally:
            reader.db.close()

    def run_round(self, spawn_argv_result, cli_patches=None):
        """Drive one operator_round command through the live executor with a
        stubbed spawn; returns (command_row, captured_argv)."""
        home = tempfile.mkdtemp(prefix='op-exec-')
        store = op_store.open_store(home)
        item, _ = op_store.put_signal(store, source='manual', kind='manual',
                                      key='k1', payload={}, now=1.0)
        row = store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                               (item['id'],)).fetchone()
        store.enqueue_command('operator_round', number=row['rowid'], agent='claude')
        pending = store.pending_commands()[0]
        machine = socket.gethostname()
        claimed = store.mark_dispatched(pending['id'], machine)
        captured = {}

        def fake_spawn(cfg, state_dir, prompt, dry_run, **kw):
            captured['argv'] = cfg['spawn']['command']
            return {'pid': None, 'argv': captured['argv'], 'log': None,
                    'proc': None}

        agent = SimpleNamespace(spawn_argv_fn=lambda a, m=None: spawn_argv_result)
        cli_patch = mock.patch.multiple('codeloop.cli', **(cli_patches or {})) \
            if cli_patches else mock.patch.object(round_module.trigger,
                                                  'DEFAULT_COMMAND',
                                                  list(trigger.DEFAULT_COMMAND))
        try:
            with mock.patch.object(round_module.op_store, 'open_store',
                                   lambda: store), \
                 mock.patch.object(round_module.trigger, 'spawn_worker',
                                   fake_spawn), \
                 cli_patch, \
                 mock.patch('codeloop.agent_runners.opencode.native_exe_for',
                            side_effect=lambda path: 'C:/fake/native/claude.exe'
                            if path.endswith('claude.CMD') else None):
                round_module.execute_operator_round(agent, {
                    'id': claimed['id'], 'action': 'operator_round',
                    'number': claimed['number'], 'item': item['id'],
                    'agent': 'claude'})
        finally:
            store.db.close()
        reader = op_store.open_store(home)
        try:
            cmd = reader.db.execute('SELECT * FROM commands WHERE id=?',
                                    (claimed['id'],)).fetchone()
            return dict(cmd), captured['argv']
        finally:
            reader.db.close()

    def test_explicit_argv_is_used(self):
        argv = ['echo', 'preset']
        cmd, used = self.run_round(argv)
        self.assertEqual(cmd['state'], 'SPAWNED', cmd['fail_reason'])
        self.assertEqual(used, argv)

    def test_none_argv_resolves_default_through_native_exe(self):
        """Production catch 2026-10-08: bare `claude` resolves to the npm
        .CMD shim and `cmd.exe /c <shim>` + DETACHED exits 1 with zero
        output — the opencode silent-death lesson. The operator fallback
        must bind the native exe, like every working loop registration."""
        cmd, argv = self.run_round(None, cli_patches={
            'resolve_command': lambda argv: ['cmd.exe', '/c',
                                             'C:/fake/npm/claude.CMD'] + argv[1:],
        })
        self.assertEqual(cmd['state'], 'SPAWNED', cmd['fail_reason'])
        self.assertEqual(argv, ['C:/fake/native/claude.exe',
                                '-p', '--dangerously-skip-permissions'])

    def test_fallback_without_resolvable_native_keeps_resolved_argv(self):
        """A shim whose target cannot be resolved degrades honestly to the
        resolved argv (no invention) — spawn may fail loudly downstream."""
        cmd, argv = self.run_round(None, cli_patches={
            'resolve_command': lambda argv: ['cmd.exe', '/c',
                                             'C:/fake/npm/missing.CMD'] + argv[1:],
        })
        self.assertEqual(cmd['state'], 'SPAWNED', cmd['fail_reason'])
        self.assertEqual(argv, ['cmd.exe', '/c', 'C:/fake/npm/missing.CMD',
                                '-p', '--dangerously-skip-permissions'])


if __name__ == '__main__':
    unittest.main()
