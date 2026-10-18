#!/usr/bin/env python3
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp import runtime
from codeloop.cp.runtime import load_config, version, validate_worktree, run_checks


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL, text=True).strip()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.email', 'test@example.invalid')
        git(self.repo, 'config', 'user.name', 'Test')
        (self.repo / 'check.py').write_text('raise SystemExit(1)\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.wt = self.root / 'trees' / 'issue-1'
        git(self.repo, 'worktree', 'add', '-b', 'feature/mocx/test', str(self.wt))
        self.config = {'repo_root': str(self.repo), 'worktree_root': str(self.root / 'trees'),
                       'branch_prefixes': ['feature/mocx/', 'fix/mocx/'], 'base': 'main',
                       'verification': [[sys.executable, '{worktree}/check.py']], 'verify_timeout': 10}

    def tearDown(self):
        self.tmp.cleanup()

    def test_verify_subprocess_env_strips_pythonhome_and_pythonpath(self):
        """issue #173 回归：verify subprocess 必须剔除 PYTHONHOME/PYTHONPATH。

        继承被污染的 PYTHONHOME（指向另一 CPython）会让 venv 解释器加载
        外部 stdlib → SRE module mismatch 启动即崩。验证 run_checks 传给
        verify subprocess 的 env 不含这两个变量。"""
        import os
        import unittest.mock as mock
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        verify_env = {}
        real_run = subprocess.run

        def spy(argv, **kwargs):
            # 只记录 verify 子进程（脚本在 worktree 内）的调用，git() 的
            # capture_output 调用不在此列
            if any(str(a).endswith('check.py') for a in argv):
                verify_env.update(kwargs.get('env') or {})
            return real_run(argv, **kwargs)

        with mock.patch.dict(os.environ, {'PYTHONHOME': 'C:/fake/python312',
                                          'PYTHONPATH': 'C:/fake/site'}):
            with mock.patch.object(subprocess, 'run', side_effect=spy):
                run_checks(self.config, context, self.root / 'env-sanitize')
        self.assertTrue(verify_env, 'run_checks 必须给 verify subprocess 显式传 env')
        self.assertNotIn('PYTHONHOME', verify_env)
        self.assertNotIn('PYTHONPATH', verify_env)
        self.assertNotIn('PYTHONHOME', os.environ)  # runner 自身环境不受影响（patch 已恢复）

    def test_verify_subprocess_env_drops_uv_internal_pythonhome(self):
        """issue #267 回归：verify subprocess 必须剔除 uv 注入的
        UV_INTERNAL__PYTHONHOME。

        shim 经 uv 的 python shim 启动时会把 UV_INTERNAL__PYTHONHOME 指向
        uv 托管的 CPython 根；verify 子进程（项目 venv 解释器）继承它后同样
        加载外部 stdlib 启动即崩。该变量与 PYTHONHOME 同源不同名，
        必须一并剔除，且不能依赖 shim 行为。"""
        import os
        import unittest.mock as mock
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        verify_env = {}
        real_run = subprocess.run

        def spy(argv, **kwargs):
            if any(str(a).endswith('check.py') for a in argv):
                verify_env.update(kwargs.get('env') or {})
            return real_run(argv, **kwargs)

        with mock.patch.dict(os.environ, {'PYTHONHOME': 'C:/fake/python312',
                                          'UV_INTERNAL__PYTHONHOME': 'C:/uv/cpython312'}):
            with mock.patch.object(subprocess, 'run', side_effect=spy):
                run_checks(self.config, context, self.root / 'env-sanitize-uv')
        self.assertTrue(verify_env, 'run_checks 必须给 verify subprocess 显式传 env')
        self.assertNotIn('UV_INTERNAL__PYTHONHOME', verify_env)
        self.assertNotIn('PYTHONHOME', verify_env)
        self.assertNotIn('UV_INTERNAL__PYTHONHOME', os.environ)  # runner 自身不受影响

    def test_runner_uses_worktree_script_and_records_failure(self):
        (self.wt / 'check.py').write_text('print("worktree verified")\n')
        git(self.wt, 'add', '.')
        git(self.wt, 'commit', '-m', 'pass')
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        result = run_checks(self.config, context, self.root / 'evidence')
        self.assertTrue(result['passed'])
        self.assertIn('worktree verified', Path(result['checks'][0]['log']).read_text())
        self.assertEqual(result['head'], git(self.wt, 'rev-parse', 'HEAD'))
        self.config['verification'] = [[sys.executable, '-c', 'raise SystemExit(3)']]
        self.assertFalse(run_checks(self.config, context, self.root / 'failure')['passed'])

    def test_rejects_main_dirty_wrong_branch_and_script_escape(self):
        with self.assertRaises(ValueError):
            validate_worktree(self.config, str(self.repo), 'main')
        with self.assertRaises(ValueError):
            validate_worktree(self.config, str(self.wt), 'feature/mocx/wrong')
        (self.wt / 'untracked').write_text('dirty')
        with self.assertRaises(ValueError):
            validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        (self.wt / 'untracked').unlink()
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['verification'] = [[sys.executable, str(self.repo / 'check.py')]]
        with self.assertRaises(ValueError):
            run_checks(self.config, context, self.root / 'evidence')

    def test_rejects_human_named_worktree(self):
        """issue #250: a worktree named after its branch (a human worktree)
        must never be bound — the slug collision used to let rounds claim and
        force-delete it."""
        human = self.root / 'trees' / 'dash-execution-history'
        git(self.repo, 'worktree', 'add', '-b', 'feature/mocx/dash-execution-history',
            str(human))
        with self.assertRaises(ValueError) as caught:
            validate_worktree(self.config, str(human), 'feature/mocx/dash-execution-history')
        self.assertIn('WORKTREE_OWNERSHIP_REJECTED', str(caught.exception))

    def test_timeout_and_head_mutation_fail(self):
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['verification'] = [[sys.executable, '-c', 'import time; time.sleep(2)']]
        self.config['verify_timeout'] = 0.05
        self.assertFalse(run_checks(self.config, context, self.root / 'timeout')['passed'])
        self.config['verify_timeout'] = 10
        self.config['verification'] = [['git', 'commit', '--allow-empty', '-m', 'changed']]
        self.assertFalse(run_checks(self.config, context, self.root / 'changed')['passed'])

    def test_forward_slash_git_output_accepted(self):
        # Git prints forward slashes even when the OS separator differs (Windows).
        import unittest.mock as mock
        import codeloop.cp.runtime as runtime
        real = runtime.git

        def msys_git(root, *args):
            return real(root, *args).replace('\\', '/')

        with mock.patch.object(runtime, 'git', msys_git):
            context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.assertEqual(context['branch'], 'feature/mocx/test')
        self.assertEqual(context['head'], git(self.wt, 'rev-parse', 'HEAD'))

    def test_version_includes_policy_and_external_comment_not_own_marker(self):
        a = {'head': 'a', 'comments': []}
        self.assertNotEqual(version(a, {'base': 'a'}), version(a, {'base': 'b'}))
        self.assertNotEqual(version(a, {}), version({'head': 'a', 'comments': [{'body': 'risk'}]}, {}))

    def test_config_resolves_global_layout_and_repo_from_cwd(self):
        config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        config_dir.mkdir(parents=True)
        target = config_dir / 'control-plane.json'
        target.write_text(json.dumps({'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'},
                                      'state_dir': 'state', 'worktree_root': 'worktrees'}))
        cfg = load_config(target, cwd=str(self.repo))
        # The repo is derived from the working directory, never named by the config.
        self.assertEqual(cfg['repo_root'], str(self.repo.resolve()))
        self.assertEqual(cfg['state_dir'], str((config_dir / 'state').resolve()))
        self.assertEqual(cfg['worktree_root'], str((config_dir / 'worktrees').resolve()))

    def test_config_defaults_state_to_config_dir_and_worktrees_to_repo(self):
        config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        config_dir.mkdir(parents=True)
        target = config_dir / 'control-plane.json'
        target.write_text(json.dumps({'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}}))
        cfg = load_config(target, cwd=str(self.repo))
        self.assertEqual(cfg['state_dir'], str((config_dir / 'state').resolve()))
        # Worktrees default next to the project itself, never into the config home.
        self.assertEqual(cfg['worktree_root'], str((self.repo / '.git-worktrees').resolve()))

    def test_lease_seconds_defaults_to_four_hours_and_enforces_floor(self):
        config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        config_dir.mkdir(parents=True)
        target = config_dir / 'control-plane.json'
        base = {'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}}
        target.write_text(json.dumps(base))
        self.assertEqual(14400, load_config(target, cwd=str(self.repo))['lease_seconds'])
        target.write_text(json.dumps(dict(base, lease_seconds=1800)))
        with self.assertRaisesRegex(ValueError, 'lease_seconds'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, lease_seconds=3600)))
        self.assertEqual(3600, load_config(target, cwd=str(self.repo))['lease_seconds'])

    def test_orphan_idle_seconds_default_floor_and_off_switch(self):
        config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        config_dir.mkdir(parents=True)
        target = config_dir / 'control-plane.json'
        base = {'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}}
        target.write_text(json.dumps(base))
        self.assertEqual(7200, load_config(target, cwd=str(self.repo))['orphan_idle_seconds'])
        target.write_text(json.dumps(dict(base, orphan_idle_seconds=599)))
        with self.assertRaisesRegex(ValueError, 'orphan_idle_seconds'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, orphan_idle_seconds=True)))
        with self.assertRaisesRegex(ValueError, 'orphan_idle_seconds'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, orphan_idle_seconds='x')))
        with self.assertRaisesRegex(ValueError, 'INVALID_CONFIG'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, orphan_idle_seconds=0)))
        self.assertEqual(0, load_config(target, cwd=str(self.repo))['orphan_idle_seconds'])
        target.write_text(json.dumps(dict(base, orphan_idle_seconds=600)))
        self.assertEqual(600, load_config(target, cwd=str(self.repo))['orphan_idle_seconds'])

    def test_config_pins_platform_ca_file_and_fails_when_missing(self):
        config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        (config_dir / 'certs').mkdir(parents=True)
        (config_dir / 'certs' / 'gitea.pem').write_text('-----BEGIN CERTIFICATE-----\n')
        target = config_dir / 'control-plane.json'
        base = {'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}}
        target.write_text(json.dumps(dict(base, platform=dict(base['platform'], ca_file='certs/gitea.pem'))))
        cfg = load_config(target, cwd=str(self.repo))
        self.assertEqual(cfg['platform']['ca_file'], str((config_dir / 'certs' / 'gitea.pem').resolve()))
        target.write_text(json.dumps(dict(base, platform=dict(base['platform'], ca_file='certs/missing.pem'))))
        with self.assertRaises(ValueError):
            load_config(target, cwd=str(self.repo))

    def test_config_interpreter_validated_and_defaulted(self):
        config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        config_dir.mkdir(parents=True)
        target = config_dir / 'control-plane.json'
        base = {'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}}
        target.write_text(json.dumps(dict(base, interpreter='venv/Scripts/python.exe')))
        with self.assertRaisesRegex(ValueError, 'INTERPRETER_MISSING'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, interpreter=str(self.root / 'nope' / 'python.exe'))))
        with self.assertRaisesRegex(ValueError, 'INTERPRETER_MISSING'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, interpreter=7)))
        with self.assertRaisesRegex(ValueError, 'INVALID_CONFIG'):
            load_config(target, cwd=str(self.repo))
        target.write_text(json.dumps(dict(base, interpreter=sys.executable)))
        self.assertEqual(load_config(target, cwd=str(self.repo))['interpreter'], sys.executable)
        target.write_text(json.dumps(base))
        self.assertIsNone(load_config(target, cwd=str(self.repo))['interpreter'])

    def commit_worktree_script(self, name, body):
        (self.wt / name).write_text(body)
        git(self.wt, 'add', '.')
        git(self.wt, 'commit', '-m', 'add ' + name)

    def test_python_placeholder_prefers_configured_interpreter(self):
        self.commit_worktree_script('show_interp.py', 'import sys; print(sys.executable)\n')
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['interpreter'] = sys.executable
        self.config['verification'] = [['{python}', '{worktree}/show_interp.py']]
        check = run_checks(self.config, context, self.root / 'interp-pinned')['checks'][0]
        self.assertEqual(check['argv'][0], sys.executable)
        self.assertEqual(check['interpreter'], sys.executable)
        log = Path(check['log']).read_text()
        self.assertIn('# verify interpreter: ', log)
        self.assertNotIn('WARN', log)

    def test_python_placeholder_falls_back_to_runner_with_warning(self):
        self.commit_worktree_script('show_interp.py', 'import sys; print(sys.executable)\n')
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['verification'] = [['{python}', '{worktree}/show_interp.py']]
        check = run_checks(self.config, context, self.root / 'interp-fallback')['checks'][0]
        self.assertEqual(check['argv'][0], sys.executable)
        self.assertEqual(check['interpreter'], sys.executable)
        log = Path(check['log']).read_text()
        self.assertIn('# verify interpreter: ' + sys.executable, log)
        self.assertIn('WARN', log)

    def test_failed_nodes_extracted_from_pytest_summary(self):
        # Baseline-derivation input: the pytest short-summary lines must ride
        # the evidence so the coordinator never needs the raw log back.
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['verification'] = [[sys.executable, '-c',
            'print("FAILED tests/a.py::t1 - NameError: x");'
            'print("ERROR tests/b.py::t2 - collect error");'
            'print("FAILED tests/a.py::t1 - duplicate ignored");'
            'raise SystemExit(1)']]
        check = run_checks(self.config, context, self.root / 'nodes-ok')['checks'][0]
        self.assertEqual(check['failed_nodes'], ['tests/a.py::t1', 'tests/b.py::t2'])
        self.assertTrue(check['failed_line'].startswith('FAILED tests/a.py::t1'))

    def test_failed_nodes_absent_for_pass_env_and_plain_failure(self):
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        cases = [
            (['-c', 'print("FAILED tests/a.py::t1 - x")'], 'pass'),
            (['-c', 'print("FAILED tests/a.py::t1 - x");'
                    'raise ModuleNotFoundError("No module named \'radon\'")'], 'env'),
            (['-c', 'raise SystemExit(1)'], 'code'),
        ]
        for i, (args, expected_class) in enumerate(cases):
            self.config['verification'] = [[sys.executable] + args]
            check = run_checks(self.config, context, self.root / ('nodes-absent-%d' % i))['checks'][0]
            self.assertEqual(check['failure_class'], expected_class, args)
            self.assertNotIn('failed_nodes', check, args)
            self.assertNotIn('failed_line', check, args)

    def test_failed_nodes_capped_at_fifty(self):
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['verification'] = [[sys.executable, '-c',
            'print("FAILED " + "tests/long.py::t" + "x"*300 + " - pathological one-line node");'
            'print("\\n".join("FAILED tests/f%d.py::t%d - x" % (i, i) for i in range(60)));'
            'raise SystemExit(1)']]
        check = run_checks(self.config, context, self.root / 'nodes-cap')['checks'][0]
        self.assertEqual(len(check['failed_nodes']), 50)
        # Node ids and sample lines ride the evidence into payloads — keep
        # both bounded against pathological one-line logs.
        self.assertEqual(len(check['failed_line']), 200)
        self.assertEqual(len(check['failed_nodes'][0]), 200)

    def test_failed_nodes_survive_timeout(self):
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        self.config['verification'] = [[sys.executable, '-u', '-c',
            'print("FAILED tests/slow.py::t - x"); import time; time.sleep(2)']]
        # 0.5s: tight but leaves Windows process startup room to flush the
        # summary line before the kill — 0.05s races the interpreter boot.
        self.config['verify_timeout'] = 0.5
        check = run_checks(self.config, context, self.root / 'nodes-timeout')['checks'][0]
        self.assertEqual(check['failure_class'], 'timeout')
        self.assertEqual(check['failed_nodes'], ['tests/slow.py::t'])

    def test_check_failure_classification_and_env_signature(self):
        context = validate_worktree(self.config, str(self.wt), 'feature/mocx/test')
        cases = [
            ([sys.executable, '-c', 'print("ok")'], 'pass', None),
            ([sys.executable, '-c', 'raise SystemExit(1)'], 'code', None),
            ([sys.executable, '-c', "raise ModuleNotFoundError(\"No module named 'radon'\")"],
             'env', 'no-module:radon'),
            ([sys.executable, '-c', "raise ImportError(\"cannot import name 'validate_core_schema'"
                                   " from 'pydantic_core'\")"],
             'env', 'import-name:pydantic_core.validate_core_schema'),
            ([sys.executable, '-c', 'raise ImportError("broken internal import")'], 'env', None),
            ([sys.executable, '-c', "raise AssertionError('Failed: DID NOT RAISE ImportError')"],
             'code', None),
            (['definitely-not-a-real-interpreter-xyz', '-c', 'pass'], 'env', None),
        ]
        for i, (argv, expected_class, expected_signature) in enumerate(cases):
            self.config['verification'] = [argv]
            result = run_checks(self.config, context, self.root / ('classify-%d' % i))
            check = result['checks'][0]
            self.assertEqual(check['failure_class'], expected_class, argv)
            if expected_signature is not None:
                self.assertEqual(result['env_signature'], expected_signature, argv)
            elif expected_class == 'env':
                self.assertTrue(result['env_signature'].startswith('env:'), argv)
            else:
                self.assertIsNone(result['env_signature'], argv)
        self.config['verification'] = [[sys.executable, '-c', 'import time; time.sleep(2)']]
        self.config['verify_timeout'] = 0.05
        result = run_checks(self.config, context, self.root / 'classify-timeout')
        self.assertEqual(result['checks'][0]['failure_class'], 'timeout')
        self.config['verify_timeout'] = 10
        self.config['verification'] = [
            [sys.executable, '-c', 'raise SystemExit(1)'],
            [sys.executable, '-c', "raise ModuleNotFoundError(\"No module named 'radon'\")"],
        ]
        result = run_checks(self.config, context, self.root / 'classify-multi')
        self.assertEqual(result['checks'][0]['failure_class'], 'code')
        self.assertEqual(result['checks'][1]['failure_class'], 'env')
        self.assertEqual(result['env_signature'], 'no-module:radon')
        self.assertFalse(result['passed'])


class ExamParseTests(unittest.TestCase):
    """仓库考题文件（.code-loop/verify.json）的严格解析：坏文件必须响亮失败。"""

    def test_accepts_the_two_policy_keys(self):
        exam = runtime.parse_exam(json.dumps({'verification': [['{python}', '-m', 'pytest', '-q']],
                                              'required_checks': ['ci/build']}))
        self.assertEqual([['{python}', '-m', 'pytest', '-q']], exam['verification'])
        self.assertEqual(['ci/build'], exam['required_checks'])

    def test_absent_keys_default_to_an_empty_exam(self):
        # 未声明 verification_fast 时键不存在（零扰动：policy hash 逐字节不变）
        self.assertEqual({'verification': [], 'required_checks': []}, runtime.parse_exam('{}'))

    def test_accepts_verification_fast_tier(self):
        """加速层考题（2026-09-21）：与全量同构同校验；声明才出键。"""
        exam = runtime.parse_exam(json.dumps({
            'verification': [['{python}', 'verify_skill.py']],
            'verification_fast': [['{python}', 'verify_skill.py', '--base', 'origin/main']]}))
        self.assertEqual([['{python}', 'verify_skill.py', '--base', 'origin/main']],
                         exam['verification_fast'])
        self.assertNotIn('verification_fast', runtime.parse_exam(
            json.dumps({'verification': [['{python}', 'verify_skill.py']]})))

    def test_rejects_non_argv_verification_fast(self):
        for body in ('{"verification_fast": "pytest -q"}',
                     '{"verification_fast": [["pytest", 1]]}', '{"verification_fast": [[]]}'):
            with self.assertRaisesRegex(ValueError, 'VERIFICATION_FAST_MUST_BE_ARGV'):
                runtime.parse_exam(body)

    def test_resolve_verification_kind_tiers(self):
        """reviewer 恒全量（合并前地板）；其余 kind 有 fast 走 fast、无 fast 回落全量。"""
        cfg = {'verification': [['full']], 'verification_fast': [['fast']]}
        self.assertEqual([['full']], runtime.resolve_verification(cfg, 'reviewer'))
        self.assertEqual([['fast']], runtime.resolve_verification(cfg, 'handler'))
        self.assertEqual([['full']],
                         runtime.resolve_verification({'verification': [['full']]}, 'handler'))
        # reviewer 即使声明了 fast 也不得走加速层
        self.assertEqual([['full']],
                         runtime.resolve_verification({'verification': [['full']],
                                                       'verification_fast': [['fast']]}, 'reviewer'))

    def test_rejects_unknown_keys(self):
        # interpreter/verify_timeout 是机器事实，不得随仓库文件走。
        with self.assertRaisesRegex(ValueError, 'EXAM_INVALID: unknown keys interpreter'):
            runtime.parse_exam(json.dumps({'verification': [], 'interpreter': 'D:/venv/python.exe'}))

    def test_rejects_non_argv_verification(self):
        for body in ('{"verification": "pytest -q"}', '{"verification": [["pytest", 1]]}',
                     '{"verification": [[]]}'):
            with self.assertRaisesRegex(ValueError, 'VERIFICATION_MUST_BE_ARGV'):
                runtime.parse_exam(body)

    def test_rejects_bad_required_checks(self):
        for body in ('{"required_checks": "ci"}', '{"required_checks": ["  "]}',
                     '{"required_checks": [7]}'):
            with self.assertRaisesRegex(ValueError, 'EXAM_INVALID: required_checks'):
                runtime.parse_exam(body)

    def test_rejects_non_json_and_non_object(self):
        with self.assertRaisesRegex(ValueError, 'EXAM_INVALID: exam is not JSON'):
            runtime.parse_exam('pytest -q\n')
        with self.assertRaisesRegex(ValueError, 'EXAM_INVALID: exam must be a JSON object'):
            runtime.parse_exam('[["pytest"]]')

    def test_rejects_oversize_and_non_text(self):
        with self.assertRaisesRegex(ValueError, 'EXAM_INVALID'):
            runtime.parse_exam('x' * (runtime.EXAM_MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'EXAM_INVALID'):
            runtime.parse_exam(None)

    def test_exam_path_is_repo_relative(self):
        self.assertEqual('.code-loop/verify.json', runtime.EXAM_PATH)


class DefaultWorkerTests(unittest.TestCase):
    def test_shape_is_hostname_slash_loop(self):
        self.assertEqual('host-a/reviewer', runtime.default_worker('reviewer', hostname='host-a'))

    def test_blank_hostname_falls_back(self):
        self.assertEqual('unknown-host/discover', runtime.default_worker('discover', hostname='   '))

    def test_default_host_is_this_machine(self):
        self.assertEqual(socket.gethostname().strip() + '/handler',
                         runtime.default_worker('handler'))


if __name__ == '__main__':
    unittest.main()
