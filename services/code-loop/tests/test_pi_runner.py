#!/usr/bin/env python3
"""pi plugin tests: the npm shim → node entry resolution, the --list-models
table parse and the spawn argv preset.

pi is a node application: the npm shim calls ``node  <…>/cli.js``, not a
native exe, so the resolution differs from opencode's (shim target is a
script that needs the node interpreter beside or on PATH). Everything is
exercised against real on-disk shim layouts, not mocks of the parser."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
for _p in (ROOT, str(Path(ROOT) / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop import agent_runners
from codeloop.agent_runners import base
from codeloop.agent_runners.pi import RUNNER, node_entry_for


def which_stub(name):
    """A CLI installed at a native (non-shim) path — the common case."""
    return 'C:/bin/' + name


def run_fn_factory(stdout='', rc=0):
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr='')
    return run


# The real npm shim layout, verbatim (%APPDATA%\npm\pi.cmd): the wrapper only
# exists to call node on the packaged cli.js — note the ``%_prog%`` variable
# target, which existence checks must never adopt.
PI_SHIM_BODY = (
    '@ECHO off\nGOTO start\n:find_dp0\nSET dp0=%~dp0\nEXIT /b\n:start\n'
    'SETLOCAL\nCALL :find_dp0\n\n'
    'IF EXIST "%dp0%\\node.exe" (\n'
    '  SET "_prog=%dp0%\\node.exe"\n'
    ') ELSE (\n'
    '  SET "_prog=node"\n'
    '  SET PATHEXT=%PATHEXT:;.JS;=;%\n'
    ')\n\n'
    'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
    '"%dp0%\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\bundle\\cli.js" %*\n')
JS_REL = 'node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js'

# Real ``pi --list-models`` output shape (0.85.1): an aligned table whose
# header tokens themselves pass a naive one-token-per-line parse.
MODELS_TABLE = (
    'provider  model                        context  max-out  thinking  images\n'
    'selfhost  deepseek-v4-flash-0731-self  300K     30K      yes       no    \n')
TOKEN = 'selfhost/deepseek-v4-flash-0731-self'


class ShimFixture:
    """A real on-disk npm install of pi: the shim text, node.exe beside it
    and the packaged cli.js — resolution exercised against files."""

    def __init__(self, body=PI_SHIM_BODY, js=True, node_beside=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.shim = self.root / 'pi.CMD'
        self.shim.write_text(body, encoding='utf-8')
        self.js = self.root / JS_REL
        if js:
            self.js.parent.mkdir(parents=True)
            self.js.write_text('', encoding='utf-8')
        self.node = self.root / 'node.exe'
        if node_beside:
            self.node.write_text('', encoding='utf-8')

    def which(self, name):
        if name == 'pi':
            return str(self.shim)
        if name == 'node':
            return 'C:/Program Files/nodejs/node.EXE'
        return None

    def cleanup(self):
        self.tmp.cleanup()


class ShimTargetsTests(unittest.TestCase):
    """base.shim_targets: every quoted shim target that resolves to a real
    file, in order — the shared skeleton both plugin policies filter."""

    def test_real_pi_shim_yields_the_cli_js(self):
        fix = ShimFixture()
        try:
            targets = base.shim_targets(str(fix.shim))
            self.assertEqual([str(fix.js)], [t for t in targets
                                             if t.lower().endswith('.js')])
        finally:
            fix.cleanup()

    def test_variable_target_is_never_adopted(self):
        fix = ShimFixture(node_beside=False)
        try:
            targets = base.shim_targets(str(fix.shim))
            self.assertNotIn(str(fix.node), targets)          # no node beside
            self.assertFalse(any('%' in t for t in targets))  # no literal var
        finally:
            fix.cleanup()

    def test_missing_target_is_not_adopted(self):
        fix = ShimFixture(js=False, node_beside=False)
        try:
            self.assertEqual([], base.shim_targets(str(fix.shim)))
        finally:
            fix.cleanup()

    def test_unreadable_shim_degrades_to_empty(self):
        def boom(_path):
            raise OSError('nope')
        self.assertEqual([], base.shim_targets('X:/npm/pi.CMD', read_fn=boom))


class NodeEntryTests(unittest.TestCase):
    """pi.node_entry_for: non-shim paths are used directly; a shim resolves
    to [node, cli.js]; anything unresolvable is None (refused before claim)."""

    def test_non_shim_path_is_used_directly(self):
        self.assertEqual(['C:/bin/pi'], node_entry_for('C:/bin/pi'))

    def test_shim_resolves_to_node_plus_cli_js(self):
        fix = ShimFixture()
        try:
            self.assertEqual([str(fix.node), str(fix.js)],
                             node_entry_for(str(fix.shim), which_fn=fix.which))
        finally:
            fix.cleanup()

    def test_node_beside_the_shim_wins(self):
        fix = ShimFixture()
        try:
            def other_node(name):
                return 'X:/elsewhere/node.exe' if name == 'node' else None
            entry = node_entry_for(str(fix.shim), which_fn=other_node)
            self.assertEqual(str(fix.node), entry[0])
        finally:
            fix.cleanup()

    def test_node_only_on_path(self):
        fix = ShimFixture(node_beside=False)
        try:
            entry = node_entry_for(str(fix.shim), which_fn=fix.which)
            self.assertEqual(['C:/Program Files/nodejs/node.EXE', str(fix.js)],
                             entry)
        finally:
            fix.cleanup()

    def test_missing_cli_js_refuses(self):
        fix = ShimFixture(js=False)
        try:
            self.assertIsNone(node_entry_for(str(fix.shim),
                                             which_fn=fix.which))
        finally:
            fix.cleanup()

    def test_missing_node_refuses(self):
        fix = ShimFixture(node_beside=False)
        try:
            def no_node(name):
                return str(fix.shim) if name == 'pi' else None
            self.assertIsNone(node_entry_for(str(fix.shim), which_fn=no_node))
        finally:
            fix.cleanup()


class PiModelsTests(unittest.TestCase):
    """The --list-models table parse: provider+model columns joined, header
    skipped, every failure a degraded empty list."""

    def test_parses_the_table_skipping_the_header(self):
        models = RUNNER.models(run_fn=run_fn_factory(MODELS_TABLE),
                               which_fn=which_stub)
        self.assertEqual([TOKEN], models)

    def test_model_column_with_a_slash_is_used_verbatim(self):
        table = ('provider  model                context\n'
                 'selfhost  other/vendor-model  300K\n')
        self.assertEqual(['other/vendor-model'],
                         RUNNER.models(run_fn=run_fn_factory(table),
                                       which_fn=which_stub))

    def test_summary_rows_never_enter_the_picker(self):
        """A real row carries six columns; short junk lines (summaries,
        blanks) are not model rows and must not yield provider/model pairs."""
        table = ('provider  model    context\n'
                 'selfhost  deepseek  300K\n'
                 '2 models\n')
        self.assertEqual(['selfhost/deepseek'],
                         RUNNER.models(run_fn=run_fn_factory(table),
                                       which_fn=which_stub))

    def test_nonzero_exit_degrades_to_empty(self):
        self.assertEqual([], RUNNER.models(run_fn=run_fn_factory(rc=1),
                                           which_fn=which_stub))

    def test_missing_binary_degrades_to_empty(self):
        self.assertEqual([], RUNNER.models(run_fn=run_fn_factory(MODELS_TABLE),
                                           which_fn=lambda name: None))

    def test_header_only_is_empty(self):
        table = 'provider  model  context\n'
        self.assertEqual([], RUNNER.models(run_fn=run_fn_factory(table),
                                           which_fn=which_stub))


class PiArgvTests(unittest.TestCase):
    """The spawn preset: node entry + print mode, stdin carries the prompt;
    a requested model re-checks THIS machine's table first."""

    def test_preset_without_model(self):
        argv = RUNNER.argv(which_fn=which_stub)
        self.assertEqual(['C:/bin/pi', '-p'], argv)

    def test_preset_with_model(self):
        argv = RUNNER.argv(TOKEN, which_fn=which_stub,
                           run_fn=run_fn_factory(MODELS_TABLE))
        self.assertEqual(['C:/bin/pi', '-p', '--model', TOKEN], argv)

    def test_shim_resolves_to_the_node_entry(self):
        fix = ShimFixture()
        try:
            argv = RUNNER.argv(which_fn=fix.which)
            self.assertEqual([str(fix.node), str(fix.js), '-p'], argv)
        finally:
            fix.cleanup()

    def test_unavailable_model_refuses_before_spawn(self):
        with self.assertRaises(ValueError) as caught:
            RUNNER.argv('a/b', which_fn=which_stub,
                        run_fn=run_fn_factory(MODELS_TABLE))
        self.assertIn('MODEL_UNAVAILABLE', str(caught.exception))

    def test_invalid_model_id_is_refused_without_detection(self):
        def explode(cmd, **kw):
            raise AssertionError('detection must not run for a bad charset')
        with self.assertRaises(ValueError) as caught:
            RUNNER.argv('bad model!', which_fn=which_stub, run_fn=explode)
        self.assertIn('INVALID_COMMAND', str(caught.exception))

    def test_missing_agent_is_loud(self):
        with self.assertRaises(ValueError) as caught:
            RUNNER.argv(which_fn=lambda name: None)
        self.assertIn('AGENT_MISSING', str(caught.exception))

    def test_shim_without_a_node_entry_refuses_before_claim(self):
        fix = ShimFixture(js=False)
        try:
            with self.assertRaises(ValueError) as caught:
                RUNNER.argv(which_fn=fix.which)
            self.assertIn('PI_SHIM_ONLY', str(caught.exception))
        finally:
            fix.cleanup()


class PiRegistryTests(unittest.TestCase):
    """Registration: the four registry faces dispatch pi like any plugin."""

    def tearDown(self):
        agent_runners.reset_cache()

    def test_pi_is_registered(self):
        self.assertIn('pi', agent_runners.ids())

    def test_pi_keeps_transcript_binding_not_stream_log(self):
        """pi rounds bind their session file through the plugin (the richer,
        content-exact stream); adding pi to STREAM_LOG_AGENTS would silently
        switch them to the spawn log instead."""
        from codeloop import agent
        self.assertNotIn('pi', agent.STREAM_LOG_AGENTS)

    def test_session_reader_dispatches_pi(self):
        self.assertEqual('pi', agent_runners.session_reader('pi').id)

    def test_validated_accepts_pi_with_model(self):
        self.assertEqual(('pi', TOKEN),
                         agent_runners.validated('pi', TOKEN))

    def test_dialog_payload_carries_pi(self):
        payload = agent_runners.options(now=1.0, run_fn=run_fn_factory(''),
                                        ttl=10 ** 9)
        card = next(c for c in payload['agents'] if c['id'] == 'pi')
        self.assertEqual('pi coding agent', card['sub'])


if __name__ == '__main__':
    unittest.main()
