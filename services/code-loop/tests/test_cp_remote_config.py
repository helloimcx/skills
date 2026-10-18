#!/usr/bin/env python3
"""Worker config = one global file per machine; policy authority stays on the
coordinator and travels only through claim responses."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import load_config, load_global_worker_config, load_server_config


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL, text=True).strip()


class GlobalWorkerConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'code-loop'
        self.home.mkdir(parents=True)
        self.global_path = self.home / 'control-plane.json'
        # A worker machine's clone: normal layout, <org>/<repo> origin.
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@i')
        git(self.repo, 'config', 'user.name', 'T')
        (self.repo / 'f').write_text('x\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')

    def tearDown(self):
        self.tmp.cleanup()

    def write_global(self, cfg):
        self.global_path.write_text(json.dumps(cfg), encoding='utf-8')

    def set_origin(self, url):
        try:
            git(self.repo, 'remote', 'add', 'origin', url)
        except subprocess.CalledProcessError:
            git(self.repo, 'remote', 'set-url', 'origin', url)

    def test_project_key_derived_from_origin_and_defaults_land_in_home(self):
        self.set_origin('https://gitea.invalid/a/b.git')
        self.write_global({'server': {'url': 'http://127.0.0.1:8722'}})
        cfg = load_global_worker_config(self.global_path, cwd=str(self.repo))
        self.assertEqual(cfg['mode'], 'worker')
        self.assertEqual(cfg['project'], 'a__b')
        self.assertEqual(cfg['server']['project'], 'a__b')
        self.assertEqual(cfg['server']['token_env'], 'CODE_LOOP_CP_TOKEN')
        self.assertEqual(cfg['server']['timeout'], 30)
        config_dir = self.home / 'projects' / 'a__b'
        self.assertEqual(cfg['state_dir'], str((config_dir / 'state').resolve()))
        # Without an explicit machine field, worktrees live next to the repo.
        self.assertEqual(cfg['worktree_root'], str((self.repo / '.git-worktrees').resolve()))
        self.assertEqual(cfg['repo_root'], str(self.repo.resolve()))
        # Policy is not config: it arrives with each claim and is cached.
        self.assertNotIn('base', cfg)
        self.assertNotIn('verification', cfg)

    def test_scp_style_origin_supported_and_project_field_overrides(self):
        self.set_origin('git@gitea.invalid:a/b.git')
        self.write_global({'server': {'url': 'http://127.0.0.1:8722'}})
        self.assertEqual(load_global_worker_config(self.global_path, cwd=str(self.repo))['project'], 'a__b')
        # A pinned project wins over derivation (mirrors the enrolled grant).
        self.set_origin('https://gitea.invalid/a/b.git')
        self.write_global({'server': {'url': 'http://127.0.0.1:8722', 'project': 'a__b'}})
        self.assertEqual(load_global_worker_config(self.global_path, cwd=str(self.repo))['project'], 'a__b')

    def test_local_path_origin_cannot_derive_a_key(self):
        bare = self.root / 'origin.git'
        git(self.repo, 'init', '--bare', str(bare))
        git(self.repo, 'remote', 'add', 'origin', str(bare))
        self.write_global({'server': {'url': 'http://127.0.0.1:8722'}})
        with self.assertRaisesRegex(ValueError, 'PROJECT_KEY_UNRESOLVED'):
            load_global_worker_config(self.global_path, cwd=str(self.repo))

    def test_machine_and_project_overrides_layer(self):
        self.set_origin('https://gitea.invalid/a/b.git')
        wide_python = self.root / 'py-wide' / 'python.exe'
        wide_python.parent.mkdir(parents=True)
        wide_python.write_text('')
        proj_python = self.root / 'py-proj' / 'python.exe'
        proj_python.parent.mkdir(parents=True)
        proj_python.write_text('')
        self.write_global({
            'server': {'url': 'http://127.0.0.1:8722'},
            'interpreter': str(wide_python),
            'worktree_root': str(self.root / 'wide-trees'),
            'projects': {'a__b': {'interpreter': str(proj_python),
                                  'worktree_root': str(self.root / 'proj-trees')}},
        })
        cfg = load_global_worker_config(self.global_path, cwd=str(self.repo))
        # _pin_interpreter normalizes without resolving links; compare likewise
        # (Path.resolve would expand the 8.3 temp-dir short name on Windows).
        self.assertEqual(cfg['interpreter'], os.path.abspath(str(proj_python)))
        self.assertEqual(cfg['worktree_root'], str((self.root / 'proj-trees').resolve()))
        # Another project on the same machine falls back to machine-wide fields.
        self.write_global({
            'server': {'url': 'http://127.0.0.1:8722', 'project': 'c__d'},
            'interpreter': str(wide_python),
            'worktree_root': str(self.root / 'wide-trees'),
            'projects': {'a__b': {'interpreter': str(proj_python)}},
        })
        cfg = load_global_worker_config(self.global_path, cwd=str(self.repo))
        self.assertEqual(cfg['worktree_root'], str((self.root / 'wide-trees').resolve()))
        self.assertEqual(cfg['interpreter'], os.path.abspath(str(wide_python)))

    def test_missing_global_file_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'GLOBAL_WORKER_CONFIG_MISSING'):
            load_global_worker_config(self.home / 'absent.json', cwd=str(self.repo))

    def test_verify_timeout_inherited_from_project_config(self):
        """issue #200：全局未显式给出 verify_timeout 时缺省继承项目 config 值，
        避免同一条 verify 命令因解析 personality 不同拿到不同预算。"""
        self.set_origin('https://gitea.invalid/a/b.git')
        self.write_global({'server': {'url': 'http://127.0.0.1:8722'}})
        project_dir = self.home / 'projects' / 'a__b'
        project_dir.mkdir(parents=True)
        (project_dir / 'control-plane.json').write_text(
            json.dumps({'verify_timeout': 600}), encoding='utf-8')
        cfg = load_global_worker_config(self.global_path, cwd=str(self.repo))
        self.assertEqual(cfg['verify_timeout'], 600)

    def test_verify_timeout_global_override_wins(self):
        """全局 projects.<key> override 优先于项目 config 继承。"""
        self.set_origin('https://gitea.invalid/a/b.git')
        self.write_global({'server': {'url': 'http://127.0.0.1:8722'},
                           'projects': {'a__b': {'verify_timeout': 900}}})
        project_dir = self.home / 'projects' / 'a__b'
        project_dir.mkdir(parents=True)
        (project_dir / 'control-plane.json').write_text(
            json.dumps({'verify_timeout': 300}), encoding='utf-8')
        cfg = load_global_worker_config(self.global_path, cwd=str(self.repo))
        self.assertEqual(cfg['verify_timeout'], 900)

    def test_verify_timeout_absent_when_no_source(self):
        """全局与项目 config 都没有 verify_timeout：不注入（默认 120 由调用方兜底）。"""
        self.set_origin('https://gitea.invalid/a/b.git')
        self.write_global({'server': {'url': 'http://127.0.0.1:8722'}})
        cfg = load_global_worker_config(self.global_path, cwd=str(self.repo))
        self.assertNotIn('verify_timeout', cfg)

    def test_non_loopback_server_requires_tls(self):
        self.set_origin('https://gitea.invalid/a/b.git')
        self.write_global({'server': {'url': 'http://cp.lan:8722'}})
        with self.assertRaisesRegex(ValueError, 'SERVER_URL_REQUIRES_TLS'):
            load_global_worker_config(self.global_path, cwd=str(self.repo))


class PerProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@i')
        git(self.repo, 'config', 'user.name', 'T')
        (self.repo / 'f').write_text('x\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        self.config_dir.mkdir(parents=True)
        self.target = self.config_dir / 'control-plane.json'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, cfg):
        self.target.write_text(json.dumps(cfg), encoding='utf-8')

    def base_config(self):
        return {'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'},
                'base': 'main', 'verification': [['{python}', '-m', 'pytest', '-q']],
                'branch_prefixes': ['feature/mocx/', 'fix/mocx/']}

    def test_worker_routing_no_longer_lives_in_project_files(self):
        # The coordinator's per-project config never routes: a worker points at
        # the CP server through the machine-global file only.
        cfg = self.base_config()
        cfg['server'] = {'url': 'http://127.0.0.1:8722'}
        self.write(cfg)
        with self.assertRaisesRegex(ValueError, 'SERVER_FIELD_REMOVED'):
            load_config(self.target, cwd=str(self.repo))

    def test_local_construction_untouched(self):
        self.write(self.base_config())
        cfg = load_config(self.target, cwd=str(self.repo))
        self.assertEqual(cfg['mode'], 'local')
        self.assertEqual(cfg['lease_seconds'], 14400)
        self.assertEqual(cfg['verification'], [['{python}', '-m', 'pytest', '-q']])


class ServerConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / 'code-loop' / 'projects' / 'a__b'
        self.config_dir.mkdir(parents=True)
        self.target = self.config_dir / 'control-plane.json'

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, cfg):
        self.target.write_text(json.dumps(cfg), encoding='utf-8')

    def test_server_config_needs_no_repo_or_cwd(self):
        # The coordinator may host a project whose clone lives elsewhere.
        self.write({'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}, 'remote': True})
        cfg = load_server_config(self.target)
        self.assertEqual(cfg['mode'], 'local')
        self.assertTrue(cfg['remote'])
        self.assertEqual(cfg['lease_seconds'], 14400)
        self.assertEqual(cfg['state_dir'], str((self.config_dir / 'state').resolve()))
        # No repo on this machine for remote projects, so the worktree root
        # falls back to the config directory — inert: bind facts are
        # worker-attested, never path-checked here.
        self.assertEqual(cfg['worktree_root'], str((self.config_dir / 'worktrees').resolve()))
        self.assertNotIn('repo_root', cfg)

    def test_server_config_rejects_worker_routing(self):
        self.write({'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'},
                    'remote': True, 'server': {'url': 'http://127.0.0.1:8722'}})
        with self.assertRaisesRegex(ValueError, 'SERVER_FIELD_ON_COORDINATOR'):
            load_server_config(self.target)

    def test_server_config_requires_platform_and_bool_remote(self):
        self.write({'remote': True})
        with self.assertRaisesRegex(ValueError, 'PLATFORM_REQUIRED'):
            load_server_config(self.target)
        self.write({'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}, 'remote': 'yes'})
        with self.assertRaisesRegex(ValueError, 'INVALID_CONFIG: remote'):
            load_server_config(self.target)

    def test_server_config_keeps_lease_floor(self):
        self.write({'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'},
                    'lease_seconds': 1800})
        with self.assertRaisesRegex(ValueError, 'lease_seconds'):
            load_server_config(self.target)


if __name__ == '__main__':
    unittest.main()
