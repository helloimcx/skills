#!/usr/bin/env python3
"""Agent-runner registry: the plugin contract, the executor's argv resolution
and the dialog's options payload. The dropdown reflects the serve host's CLI;
the verdict before a spawn belongs to the machine that pays for the session.

Spawn argv binds the NATIVE executable, never an npm .cmd shim: the shim
bridges through cmd.exe, and ``cmd /c batch`` dies silently (exit 1, zero
output) under the DETACHED_PROCESS flag every spawn uses."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = str(Path(__file__).resolve().parents[1])
for _p in (ROOT, str(Path(ROOT) / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop import agent_runners
from codeloop.agent_runners import base
from codeloop.agent_runners.opencode import native_exe_for


def run_fn_factory(stdout='', rc=0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr='')
    return run


def which_stub(name):
    """A CLI that is installed at a native path — the common case."""
    return 'C:/bin/' + name


def opencode():
    return agent_runners.REGISTRY.get('opencode')


# The real npm shim layout, verbatim (see %APPDATA%\npm\opencode.CMD):
# the batch wrapper only exists to call the native exe beside it.
SHIM_BODY = ('@ECHO off\nGOTO start\n:find_dp0\nSET dp0=%~dp0\nEXIT /b\n:start\n'
             'SETLOCAL\nCALL :find_dp0\n'
             '"%dp0%\\node_modules\\opencode-ai\\bin\\opencode.exe"   %*\n')
NATIVE_REL = 'node_modules/opencode-ai/bin/opencode.exe'


class InstallFixture:
    """A real on-disk npm install: the shim text and the native exe beside it,
    so the resolution is exercised against files, not mocks."""

    def __init__(self, body=SHIM_BODY, native=True, suffix='.CMD'):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.shim = self.root / ('opencode' + suffix)
        self.shim.write_text(body, encoding='utf-8')
        self.native = self.root / NATIVE_REL
        if native:
            self.native.parent.mkdir(parents=True)
            self.native.write_text('', encoding='utf-8')

    def which(self, name):
        return str(self.shim)

    def cleanup(self):
        self.tmp.cleanup()


class DetectModelsTests(unittest.TestCase):
    def test_parses_provider_model_lines_with_allowlist(self):
        stdout = ('opencode/big-pickle\n'
                  'kimi-for-coding/k3\n'
                  'opencode/mimo-v2.5-free\n'
                  '\n'
                  'nonsense line without slash\n'
                  'bad space/model\n'
                  '../escape/model\n'
                  'opencode/dup\n'
                  'opencode/dup\n')
        models = opencode().models(run_fn=run_fn_factory(stdout=stdout),
                                   which_fn=which_stub)
        self.assertEqual(['opencode/big-pickle', 'kimi-for-coding/k3',
                          'opencode/mimo-v2.5-free', 'opencode/dup'], models)

    def test_injected_runner_is_used_without_which_fn(self):
        calls = []
        models = opencode().models(
            run_fn=run_fn_factory(stdout='opencode/big-pickle\n', calls=calls))
        self.assertEqual(['opencode/big-pickle'], models)
        self.assertEqual(1, len(calls))

    def test_nonzero_exit_means_no_models(self):
        self.assertEqual([], opencode().models(
            run_fn=run_fn_factory(stdout='x/y', rc=1), which_fn=which_stub))

    def test_missing_binary_and_timeout_degrade_to_empty(self):
        def boom(cmd, **kw):
            raise FileNotFoundError('opencode')

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 30)
        self.assertEqual([], opencode().models(run_fn=boom, which_fn=which_stub))
        self.assertEqual([], opencode().models(run_fn=slow, which_fn=which_stub))
        # a binary that is not on PATH at all never runs anything
        self.assertEqual([], opencode().models(which_fn=lambda name: None))

    def test_windows_cmd_wrapper_bridges_through_cmd_exe(self):
        """npm installs opencode as a .cmd shim; CreateProcess cannot exec it
        directly — the same bridge the spawn path applies to argv."""
        calls = []
        opencode().models(run_fn=run_fn_factory(calls=calls),
                          which_fn=lambda name: 'C:/npm/opencode.cmd')
        argv = calls[0]
        self.assertEqual(['cmd.exe', '/c'], argv[:2])
        self.assertTrue(argv[2].lower().endswith('opencode.cmd'))

    def test_a_plugin_without_a_listing_command_answers_none_not_empty(self):
        """None and [] are different answers: "cannot be picked" versus
        "detection found nothing" — the dialog renders them differently."""
        self.assertIsNone(agent_runners.REGISTRY.get('claude').models())


class NativeExeTests(unittest.TestCase):
    """The shim → native resolution: pure allow-list text parsing of the
    wrapper npm writes, exercised against real files on disk."""

    def setUp(self):
        self.fix = InstallFixture()

    def tearDown(self):
        self.fix.cleanup()

    def test_npm_shim_resolves_to_the_native_exe_beside_it(self):
        self.assertEqual(str(self.fix.native),
                         native_exe_for(str(self.fix.shim)))

    def test_tilde_dp0_variant_resolves_too(self):
        fix = InstallFixture(body='@ECHO off\n"%~dp0\\' + NATIVE_REL.replace('/', '\\')
                                  + '" %*\n')
        try:
            self.assertEqual(str(fix.native),
                             native_exe_for(str(fix.shim)))
        finally:
            fix.cleanup()

    def test_relative_target_without_dp0_is_ignored(self):
        fix = InstallFixture(body='@ECHO off\n"node_modules\\x.exe" %*\n')
        try:
            self.assertIsNone(native_exe_for(str(fix.shim)))
        finally:
            fix.cleanup()

    def test_missing_target_is_not_adopted(self):
        fix = InstallFixture(native=False)
        try:
            self.assertIsNone(native_exe_for(str(fix.shim)))
        finally:
            fix.cleanup()

    def test_unreadable_and_non_shim_paths_return_none_or_self(self):
        self.assertIsNone(native_exe_for(str(self.fix.root / 'nope.cmd')))
        exe = str(self.fix.root / 'opencode.exe')
        self.assertEqual(exe, native_exe_for(exe))

    def test_read_failure_degrades_to_none(self):
        def boom(path):
            raise OSError('denied')
        self.assertIsNone(native_exe_for(str(self.fix.shim), read_fn=boom))


class OpencodeSpawnArgvTests(unittest.TestCase):
    """The preset must bind the native exe: the shim path is the exact spawn
    mode that died silently under DETACHED_PROCESS (exit 1, zero output)."""

    def setUp(self):
        self.fix = InstallFixture()

    def tearDown(self):
        self.fix.cleanup()

    def test_preset_without_model(self):
        argv = opencode().argv(which_fn=self.fix.which)
        self.assertEqual([str(self.fix.native), 'run', '--auto', '--format', 'json'],
                         argv)

    def test_preset_with_model(self):
        argv = opencode().argv(
            model='kimi-for-coding/k3', which_fn=self.fix.which,
            run_fn=run_fn_factory(stdout='opencode/x\nkimi-for-coding/k3\n'))
        self.assertEqual([str(self.fix.native), 'run', '--auto', '--format', 'json',
                          '-m', 'kimi-for-coding/k3'], argv)

    def test_shim_without_a_native_target_refuses_before_claim(self):
        fix = InstallFixture(native=False)
        try:
            with self.assertRaises(ValueError) as caught:
                opencode().argv(which_fn=fix.which)
            self.assertIn('OPENCODE_SHIM_ONLY', str(caught.exception))
        finally:
            fix.cleanup()

    def test_native_on_path_is_bound_directly(self):
        argv = opencode().argv(which_fn=lambda name: str(self.fix.native))
        self.assertEqual(str(self.fix.native), argv[0])

    def test_missing_agent_is_loud(self):
        with self.assertRaises(ValueError) as caught:
            opencode().argv(which_fn=lambda name: None)
        self.assertIn('AGENT_MISSING', str(caught.exception))

    def test_unavailable_model_refuses_before_spawn(self):
        with self.assertRaises(ValueError) as caught:
            opencode().argv(
                model='kimi-for-coding/k999', which_fn=self.fix.which,
                run_fn=run_fn_factory(stdout='opencode/x\n'))
        message = str(caught.exception)
        self.assertIn('MODEL_UNAVAILABLE', message)
        self.assertIn('kimi-for-coding/k999', message)
        self.assertIn('opencode/x', message)   # what IS available, for the UI

    def test_invalid_model_id_is_refused_without_detection(self):
        with self.assertRaises(ValueError) as caught:
            opencode().argv(model='a/b; rm -rf', which_fn=which_stub,
                            run_fn=run_fn_factory(stdout='a/b\n'))
        self.assertIn('INVALID_COMMAND', str(caught.exception))


class RegistryDispatchTests(unittest.TestCase):
    """The registry is the executor's entry point: it answers for every
    registered plugin and refuses anything else, before any claim is taken."""

    def test_no_agent_means_the_registered_command(self):
        self.assertIsNone(agent_runners.spawn_argv(None, None))

    def test_every_registered_plugin_resolves_through_the_registry(self):
        for runner_id in agent_runners.ids():
            with self.subTest(agent=runner_id):
                argv = agent_runners.spawn_argv(
                    runner_id, None, which_fn=which_stub,
                    run_fn=run_fn_factory(stdout='a/b\n'))
                self.assertTrue(argv is None or isinstance(argv, list))

    def test_unknown_agent_is_refused_on_the_executor_too(self):
        with self.assertRaises(ValueError) as caught:
            agent_runners.spawn_argv('codex', None)
        self.assertIn('INVALID_COMMAND', str(caught.exception))

    def test_claude_declares_the_registered_command_explicitly(self):
        """2A: naming the claude plugin asks for this machine's registered
        spawn command — not a hardcoded `claude -p`, which would silently
        override a machine registered to something else."""
        self.assertIsNone(agent_runners.spawn_argv('claude', None))
        with self.assertRaises(ValueError) as caught:
            agent_runners.spawn_argv('claude', 'a/b')
        self.assertIn('INVALID_COMMAND', str(caught.exception))


class ValidatedPairTests(unittest.TestCase):
    """The enqueue-side gate: a whitelist of registered ids plus a strict
    model-id charset — enough to pick an argv preset in code, never enough to
    carry content."""

    def test_normalizes_and_refuses(self):
        self.assertEqual((None, None), agent_runners.validated(None, None))
        self.assertEqual(('opencode', None), agent_runners.validated('opencode', None))
        self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                         agent_runners.validated('opencode', 'kimi-for-coding/k3'))
        self.assertEqual(('claude', None), agent_runners.validated('claude', None))
        for agent, model in (('codex', None), (None, 'x/y'), (7, None), ('', None),
                             ('claude', 'x/y'), ('opencode', 'a/b; rm -rf'),
                             ('opencode', 'A B'), ('opencode', '../etc/passwd'),
                             ('opencode', ''), ('opencode', 7)):
            with self.subTest(agent=agent, model=model):
                with self.assertRaises(ValueError) as caught:
                    agent_runners.validated(agent, model)
                self.assertIn('INVALID_COMMAND', str(caught.exception))


class ThirdPluginTests(unittest.TestCase):
    """The refactor's whole point: a new coding agent is one object satisfying
    the contract, and every consumer answers for it untouched — the enqueue
    whitelist, the dialog payload and the executor's argv resolution."""

    class FakeRunner(agent_runners.Runner):
        id = 'fake'
        label = 'fake'
        sub = '测试插件'
        models_command = ['fake', 'models']

        def models(self, run_fn=None, which_fn=None, timeout=None):
            return ['p/m']          # no CLI on this test machine: report directly

        def argv(self, model=None, which_fn=None, run_fn=None):
            preset = ['fake', 'run']
            if model is not None:
                preset += ['-m', model]
            return preset

    def setUp(self):
        self.registry = agent_runners.Registry()
        self.registry.register(self.FakeRunner())
        self.run = run_fn_factory(stdout='p/m\n')

    def test_the_dialog_payload_carries_the_new_plugin(self):
        payload = self.registry.options(now=1000.0, run_fn=self.run)
        self.assertEqual([{'id': 'fake', 'label': 'fake', 'sub': '测试插件',
                           'models': ['p/m']}], payload['agents'])

    def test_the_enqueue_gate_accepts_the_new_plugin(self):
        self.assertEqual(('fake', 'p/m'), self.registry.validated('fake', 'p/m'))
        self.assertEqual(('fake', None), self.registry.validated('fake', None))
        self.assertEqual(('fake', 'p/m'),
                         self.registry.validated('fake', 'p/m'))
        with self.assertRaises(ValueError):
            self.registry.validated('fake', 'p/m; rm -rf')

    def test_the_executor_resolves_the_new_plugin(self):
        self.assertEqual(['fake', 'run', '-m', 'p/m'],
                         self.registry.spawn_argv('fake', 'p/m',
                                                  which_fn=which_stub,
                                                  run_fn=self.run))
        self.assertEqual(['fake', 'run'],
                         self.registry.spawn_argv('fake', None))

    def test_the_new_plugin_is_absent_from_the_real_registry(self):
        """A fresh Registry is not the process-wide one: registering a test
        plugin must not leak into what the control plane whitelists."""
        self.assertNotIn('fake', agent_runners.ids())
        with self.assertRaises(ValueError):
            agent_runners.validated('fake', None)


class RegistryRegistrationTests(unittest.TestCase):
    def test_duplicate_id_is_refused_loudly(self):
        registry = agent_runners.Registry()
        registry.register(opencode())
        with self.assertRaises(ValueError) as caught:
            registry.register(opencode())
        self.assertIn('RUNNER_DUPLICATE', str(caught.exception))

    def test_a_plugin_without_an_id_is_refused(self):
        class Nameless(agent_runners.Runner):
            pass
        with self.assertRaises(ValueError) as caught:
            agent_runners.Registry().register(Nameless())
        self.assertIn('RUNNER_INVALID', str(caught.exception))

    def test_insertion_order_is_the_card_order(self):
        self.assertEqual(('claude', 'opencode', 'pi'), agent_runners.ids())


class PluginContractTests(unittest.TestCase):
    """What 'add a coding agent' now means: one module satisfying this
    contract, one register line. These checks are what keep a new plugin from
    needing edits in the control plane, the agent and the dashboard."""

    def test_every_plugin_declares_identity_and_capability(self):
        for runner in agent_runners.REGISTRY.all():
            with self.subTest(agent=runner.id):
                self.assertTrue(runner.label)
                self.assertTrue(runner.sub)
                self.assertEqual(runner.has_models,
                                 runner.models_command is not None)
                if runner.models_command is not None:
                    self.assertTrue(all(isinstance(x, str) and x
                                        for x in runner.models_command))

    def test_every_plugin_answers_a_spawnable_argv(self):
        for runner in agent_runners.REGISTRY.all():
            with self.subTest(agent=runner.id):
                argv = runner.argv(which_fn=which_stub,
                                   run_fn=run_fn_factory(stdout='a/b\n'))
                if argv is not None:
                    self.assertTrue(argv and all(isinstance(x, str) and x
                                                 for x in argv))

    def test_every_plugin_refuses_a_model_it_has_no_picker_for(self):
        for runner in agent_runners.REGISTRY.all():
            if runner.has_models:
                continue
            with self.subTest(agent=runner.id):
                with self.assertRaises(ValueError):
                    runner.argv('a/b', which_fn=which_stub)

    def test_an_unknown_plugin_id_is_never_dispatchable(self):
        self.assertIsNone(agent_runners.REGISTRY.get('codex'))
        self.assertIsNone(agent_runners.REGISTRY.get(7))
        self.assertIsNone(agent_runners.REGISTRY.get(None))

    def test_every_plugin_answers_the_output_face(self):
        """The fourth face: every plugin can be asked where a session's
        output lives and can tail it — an unknown execution degrades to the
        unbound placeholder, never a raise."""
        exec_id = 'a' * 32
        for runner in agent_runners.REGISTRY.all():
            with self.subTest(agent=runner.id):
                binding = runner.resolve_session('X:\\nowhere', exec_id,
                                                 1000.0)
                self.assertEqual({'bound', 'path'}, set(binding))
                result = runner.tail(binding['path'] or 'ses_missing',
                                     offset=0)
                self.assertLessEqual({'events', 'offset', 'size', 'skipped'},
                                     set(result))
                for event in result['events']:
                    self.assertEqual({'t', 'text', 'out', 'mono', 'fold',
                                      'status'}, set(event))

    def test_the_base_runner_produces_no_session_output(self):
        """A plugin that does not implement the face degrades honestly —
        exactly the unbound view the consumers already render."""
        base = agent_runners.Runner()
        self.assertEqual({'bound': None, 'path': None},
                         base.resolve_session('X:\\nowhere', 'a' * 32, 0))
        self.assertEqual([], base.tail('ses_x')['events'])
        self.assertEqual(7, base.tail('ses_x', offset=7)['offset'])


class SessionReaderTests(unittest.TestCase):
    """The output-face dispatch: which plugin reads an execution's session
    output. The default (registered-command) path must keep reading claude —
    every pre-plugin registration named claude's spawn command."""

    def test_none_means_claude(self):
        self.assertEqual('claude', agent_runners.session_reader(None).id)
        self.assertEqual('claude', agent_runners.session_reader('').id)

    def test_a_named_agent_reads_its_own_plugin(self):
        self.assertEqual('opencode',
                         agent_runners.session_reader('opencode').id)

    def test_an_unknown_agent_degrades_to_the_default_not_an_invention(self):
        self.assertEqual('claude', agent_runners.session_reader('codex').id)


class ImportDirectionTests(unittest.TestCase):
    """The registry sits in the control plane's fail-closed path, so it must
    stay a leaf module: importing it must not import the control plane (whose
    commands module imports *it* — a cycle would break the server at startup).
    """

    def test_importing_the_registry_pulls_in_no_control_plane_module(self):
        watched = ('codeloop.trigger', 'codeloop.cp.service', 'codeloop.cp.commands',
                   'codeloop.cp.store', 'codeloop.agent', 'codeloop.dash.serve')
        code = ('import sys, codeloop.agent_runners; '
                'print(",".join(m for m in %r if m in sys.modules))' % (watched,))
        done = subprocess.run([sys.executable, '-c', code], capture_output=True,
                              text=True, env=dict(os.environ, PYTHONPATH=ROOT))
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual('', done.stdout.strip(),
                         'registry import dragged in: ' + done.stdout.strip())

    def test_the_control_plane_validates_through_the_registry(self):
        """Single source: the enqueue whitelist must be the registry's own
        validator, not a second list that can drift from the plugins."""
        from codeloop.cp import commands
        self.assertIs(commands.validated_spawn_options, agent_runners.validated)

    def test_the_event_trigger_resolves_commands_through_the_leaf_module(self):
        from codeloop import cli, trigger
        self.assertIs(trigger.resolve_command, cli.resolve_command)


class OptionsEndpointPayloadTests(unittest.TestCase):
    def setUp(self):
        agent_runners.reset_cache()

    def tearDown(self):
        agent_runners.reset_cache()

    def test_options_lists_every_plugin_with_detected_models(self):
        calls = []

        def run(cmd, **kw):
            """One listing per modeled plugin: each CLI answers in its own
            dialect (opencode one token per line, pi an aligned table)."""
            calls.append(cmd)
            stdout = ('opencode/big-pickle\nkimi-for-coding/k3\n'
                      if any(a == 'models' for a in cmd)
                      else ('provider  model    context\n'
                            'selfhost  deepseek-v4  300K\n'))
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr='')

        payload = agent_runners.options(now=1000.0, run_fn=run)
        self.assertTrue(payload['ok'])
        agents = {a['id']: a for a in payload['agents']}
        self.assertEqual(['claude', 'opencode', 'pi'], sorted(agents))
        self.assertIsNone(agents['claude']['models'])   # no command, no fake data
        self.assertEqual(['opencode/big-pickle', 'kimi-for-coding/k3'],
                         agents['opencode']['models'])
        self.assertEqual(['selfhost/deepseek-v4'],
                         agents['pi']['models'])
        self.assertEqual(2, len(calls))   # one listing per modeled plugin
        # the dialog renders cards from the payload, so identity ships with it
        for row in payload['agents']:
            self.assertTrue(row['label'])
            self.assertTrue(row['sub'])

    def test_options_is_cached_within_the_ttl(self):
        calls = []
        run = run_fn_factory(stdout='opencode/x\n', calls=calls)
        agent_runners.options(now=1000.0, run_fn=run)
        agent_runners.options(now=1300.0, run_fn=run)
        agent_runners.options(now=1000.0 + agent_runners.CACHE_TTL_SECONDS - 1,
                              run_fn=run)
        self.assertEqual(2, len(calls))   # one listing per modeled plugin
        agent_runners.options(now=1000.0 + agent_runners.CACHE_TTL_SECONDS + 1,
                              run_fn=run)
        self.assertEqual(4, len(calls))

    def test_detection_failure_caches_an_empty_list_not_an_error(self):
        def boom(cmd, **kw):
            raise FileNotFoundError('opencode')
        payload = agent_runners.options(now=1000.0, run_fn=boom)
        agents = {a['id']: a['models'] for a in payload['agents']}
        self.assertEqual([], agents['opencode'])


class ModelIdCharsetTests(unittest.TestCase):
    def test_accepts_provider_model_and_refuses_everything_else(self):
        # a bare token is allowed too (the charset, not the slash, is the
        # gate — unchanged from the pre-registry validator)
        for good in ('a/b', 'opencode/big-pickle', 'kimi-for-coding/k3',
                     'A.b_c/d-e.f', 'a'):
            self.assertTrue(base.valid_model_id(good), good)
        for bad in ('', '/b', 'a b', 'a/b;rm', '../x', '-a/b', 7, None):
            self.assertFalse(base.valid_model_id(bad), bad)


if __name__ == '__main__':
    unittest.main(verbosity=2)
