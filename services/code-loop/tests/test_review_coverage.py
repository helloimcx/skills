#!/usr/bin/env python3
"""review_coverage.py 覆盖度核算脚本测试。

夹具=临时 git 仓库：main（base）提交若干文件，feature 分支改 4 类文件：
2 个代码、1 个 lockfile、1 个生成文件，验证 compute/check 与 git diff 事实一致。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_coverage.py"


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args],
                                   stderr=subprocess.DEVNULL, text=True).strip()


def run(*args, repo=None):
    """运行脚本，返回 (exit_code, stdout_json_or_text)。"""
    cmd = [sys.executable, str(SCRIPT)]
    if repo is not None:
        cmd += ['--repo', str(repo)]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = proc.stdout.strip()
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = out
    return proc.returncode, parsed


class ReviewCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'config', 'user.name', 'Test')
        for name in ('app.py', 'pkg/util.go', 'pkg/main.go', 'package-lock.json'):
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text('base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        self.base = git(repo, 'rev-parse', 'HEAD')
        # feature branch: 2 code files changed, 1 lockfile, 1 generated bundle
        git(repo, 'checkout', '-b', 'feature/mocx/change')
        for name, content in (('app.py', 'app v2\n'), ('pkg/util.go', 'util v2\n')):
            (repo / name).write_text(content)
        (repo / 'package-lock.json').write_text('lock v2\n')
        (repo / 'dist').mkdir(exist_ok=True)
        (repo / 'dist/bundle.js').write_text('bundled v2\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'change')
        self.head = git(repo, 'rev-parse', 'HEAD')
        self.repo = repo

    def tearDown(self):
        self.tmp.cleanup()

    def diff_files(self):
        out = git(self.repo, 'diff', '--name-only', self.base + '...' + self.head)
        return out.splitlines() if out else []

    # ---- compute ----

    def test_compute_total_and_files_match_git(self):
        code, data = run('compute', '--base', self.base, '--head', self.head,
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertEqual(data['total'], 4)
        self.assertEqual(sorted(f['path'] for f in data['files']),
                         sorted(self.diff_files()))
        statuses = {f['path']: f['status'] for f in data['files']}
        self.assertEqual(statuses['app.py'], 'M')
        self.assertEqual(statuses['dist/bundle.js'], 'A')

    def test_compute_auto_skip_flags_lockfile_and_dist(self):
        code, data = run('compute', '--base', self.base, '--head', self.head,
                         repo=self.repo)
        self.assertEqual(code, 0)
        rules = {s['path']: s['rule'] for s in data['auto_skip']}
        self.assertIn('package-lock.json', rules)
        self.assertIn('dist/bundle.js', rules)
        self.assertNotIn('app.py', rules)

    def test_auto_skip_suffix_and_nested_dir(self):
        """minified/map 后缀与嵌套 build 目录命中候选跳过规则。"""
        import review_coverage as rc
        self.assertIsNotNone(rc.auto_skip_for('static/app.min.js'))
        self.assertIsNotNone(rc.auto_skip_for('web/app.js.map'))
        self.assertIsNotNone(rc.auto_skip_for('web/build/assets/x.js'))
        self.assertIsNone(rc.auto_skip_for('src/main.py'))

    def test_compute_empty_diff(self):
        code, data = run('compute', '--base', self.head, '--head', self.head,
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['files'], [])
        self.assertEqual(data['auto_skip'], [])

    def test_compute_missing_ref_fails_nonzero(self):
        code, out = run('compute', '--base', 'no-such-ref', '--head', self.head,
                        repo=self.repo)
        self.assertNotEqual(code, 0)

    # ---- check ----

    def test_check_full_coverage_ok(self):
        code, data = run('check', '--base', self.base, '--head', self.head,
                         '--reviewed', 'app.py,pkg/util.go',
                         '--skip', 'package-lock.json:lockfile;dist/bundle.js:generated bundle',
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertTrue(data['ok'])
        self.assertEqual(data['total'], 4)
        self.assertEqual(data['missing'], [])
        self.assertEqual(data['rate'], 0.5)

    def test_check_missing_file_detected(self):
        code, data = run('check', '--base', self.base, '--head', self.head,
                         '--reviewed', 'app.py',
                         '--skip', 'package-lock.json:lockfile',
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertFalse(data['ok'])
        self.assertIn('pkg/util.go', data['missing'])
        self.assertIn('dist/bundle.js', data['missing'])

    def test_check_reviewed_skipped_overlap_rejected(self):
        code, data = run('check', '--base', self.base, '--head', self.head,
                         '--reviewed', 'app.py,package-lock.json',
                         '--skip', 'package-lock.json:lockfile',
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertFalse(data['ok'])

    def test_check_skip_without_reason_rejected(self):
        code, data = run('check', '--base', self.base, '--head', self.head,
                         '--reviewed', 'app.py,pkg/util.go,pkg/main.go,dist/bundle.js',
                         '--skip', 'package-lock.json',
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertFalse(data['ok'])
        # the reason-less skip surfaces as an invalid entry
        self.assertTrue(data['skipped'][0]['reason'] == '' or 'reason' not in data['skipped'][0])

    def test_check_rate_and_reviewed_passthrough(self):
        code, data = run('check', '--base', self.base, '--head', self.head,
                         '--reviewed', 'app.py,pkg/util.go',
                         '--skip', 'package-lock.json:lockfile;dist/bundle.js:generated bundle',
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertEqual(data['reviewed'], ['app.py', 'pkg/util.go'])
        self.assertEqual(len(data['skipped']), 2)
        self.assertTrue(all(s['reason'] for s in data['skipped']))

    def test_check_empty_diff_ok(self):
        code, data = run('check', '--base', self.head, '--head', self.head,
                         '--reviewed', '', '--skip', '', repo=self.repo)
        self.assertEqual(code, 0)
        self.assertTrue(data['ok'])
        self.assertEqual(data['total'], 0)
        self.assertEqual(data['rate'], 1.0)

    def test_check_outside_diff_breaks_accounting(self):
        """记账只对 diff 文件集有意义：reviewed/skipped 出现 diff 外路径 → ok=false。

        回归：曾出现 --reviewed 混入 diff 外文件时 ok=true、rate>1.0 的假绿灯，
        与 CP 门禁（|reviewed|+|skipped|==total）矛盾。
        """
        code, data = run('check', '--base', self.base, '--head', self.head,
                         '--reviewed', 'app.py,pkg/util.go,dist/bundle.js,ghost.py',
                         '--skip', 'package-lock.json:lockfile;old.py:removed elsewhere',
                         repo=self.repo)
        self.assertEqual(code, 0)
        self.assertFalse(data['ok'])
        self.assertIn('ghost.py', data['outside'])
        self.assertIn('old.py', data['outside'])

    def test_check_rate_fraction(self):
        """1/3 的 rate 取 4 位小数（浮点边界）。"""
        repo = Path(self.tmp.name) / 'frac'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'config', 'user.name', 'Test')
        (repo / 'a.py').write_text('1\n')
        (repo / 'b.py').write_text('1\n')
        (repo / 'c.py').write_text('1\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        base = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'checkout', '-b', 'feature/mocx/x')
        (repo / 'a.py').write_text('2\n')
        (repo / 'b.py').write_text('2\n')
        (repo / 'c.py').write_text('2\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'change')
        head = git(repo, 'rev-parse', 'HEAD')
        code, data = run('check', '--base', base, '--head', head,
                         '--reviewed', 'a.py',
                         '--skip', 'b.py:refactor;c.py:refactor',
                         repo=repo)
        self.assertEqual(code, 0)
        self.assertTrue(data['ok'])
        self.assertEqual(data['total'], 3)
        self.assertEqual(data['rate'], 0.3333)

    def test_check_utf8_paths(self):
        """非 ASCII（中文）路径原文输出，不被 core.quotepath 转义。"""
        repo = Path(self.tmp.name) / 'utf8'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'config', 'user.name', 'Test')
        (repo / '新建文件.py').write_text('base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        base = git(repo, 'rev-parse', 'HEAD')
        git(repo, 'checkout', '-b', 'feature/mocx/x')
        (repo / '新建文件.py').write_text('v2\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'change')
        head = git(repo, 'rev-parse', 'HEAD')
        code, data = run('check', '--base', base, '--head', head,
                         '--reviewed', '新建文件.py', '--skip', '', repo=repo)
        self.assertEqual(code, 0)
        self.assertTrue(data['ok'])
        self.assertEqual(data['reviewed'], ['新建文件.py'])


if __name__ == '__main__':
    unittest.main()
