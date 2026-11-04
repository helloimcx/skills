"""Process refresh and per-database lock lifecycle."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / 'scripts')):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import refresh, serving, store, serve
from codeloop.dash.collect_lock import DatabaseRoundLock


class CollectLockTests(unittest.TestCase):
    def test_memory_database_lock_does_not_create_a_worktree_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                lock = DatabaseRoundLock(':memory:')
                self.assertTrue(lock.acquire())
                lock.close()
                self.assertFalse(Path(tmp, ':memory:.collect.lock').exists())
            finally:
                os.chdir(previous)

    def test_lock_is_per_database_and_owner_exit_releases_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_db = Path(tmp) / 'one.sqlite'
            other_db = Path(tmp) / 'two.sqlite'
            one = DatabaseRoundLock(first_db)
            contender = DatabaseRoundLock(first_db)
            other = DatabaseRoundLock(other_db)
            self.assertTrue(one.acquire())
            self.assertFalse(contender.acquire())
            self.assertTrue(other.acquire())
            other.close()
            one.close()
            self.assertTrue(contender.acquire())
            contender.close()
            self.assertTrue(Path(str(first_db) + '.collect.lock').exists())


class RefreshProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.db = self.root / 'dashboard.sqlite'
        self.projects = self.root / 'projects'
        self.projects.mkdir()
        self.config = self.home / '.code-loop' / 'dashboard.json'
        self.config.parent.mkdir()
        self.config.write_text('{}', encoding='utf-8')
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.home), CODE_LOOP_HOME=str(self.home / '.code-loop'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_runner_executes_formal_cli_and_receives_atomic_result(self):
        child_pids = []
        real_popen = refresh.subprocess.Popen

        def observe_child(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            child_pids.append(child.pid)
            return child

        with patch.object(refresh.subprocess, 'Popen', side_effect=observe_child):
            result = refresh.run_collect(self.db, self.projects,
                                         config_path=self.config,
                                         timeout=20, env=self.env)
        self.assertEqual(1, len(child_pids))
        self.assertNotEqual(os.getpid(), child_pids[0])
        self.assertEqual({'projects': 0, 'failed': []}, result['collect'])
        self.assertIn('precompute', result)
        check = store.connect(self.db)
        try:
            self.assertEqual(2, check.execute(
                'SELECT COUNT(*) FROM serving_cache').fetchone()[0])
            self.assertEqual({'issues': [], 'prs': []}, json.loads(
                serving.lookup(check, 'items')[0].decode('utf-8')))
        finally:
            check.close()

    def test_child_observes_lock_busy_and_parent_can_run_after_release(self):
        lock = DatabaseRoundLock(self.db)
        self.assertTrue(lock.acquire())
        try:
            skipped = refresh.run_collect(self.db, self.projects,
                                          config_path=self.config, timeout=20,
                                          env=self.env)
            self.assertEqual('collect in progress', skipped['collect']['skipped'])
        finally:
            lock.close()
        result = refresh.run_collect(self.db, self.projects, config_path=self.config,
                                     timeout=20, env=self.env)
        self.assertEqual(0, result['collect']['projects'])

    def _fake_cli(self, body):
        script = self.root / 'fake_dashboard.py'
        script.write_text(body, encoding='utf-8')
        return patch.object(refresh, 'COLLECT_SCRIPT', script)

    def test_parent_kills_and_reaps_child_after_deadline(self):
        with self._fake_cli('import time\ntime.sleep(10)\n'):
            with self.assertRaisesRegex(RuntimeError, 'deadline'):
                refresh.run_collect(self.db, self.projects, config_path=self.config,
                                    timeout=1, env=self.env)

    def test_output_overflow_stops_child(self):
        body = ('import os,time\nos.write(1,b"x"*(2*1024*1024))\n'
                'time.sleep(10)\n')
        with self._fake_cli(body):
            with self.assertRaisesRegex(RuntimeError, 'output limit'):
                refresh.run_collect(self.db, self.projects, config_path=self.config,
                                    timeout=10, env=self.env)

    def test_malformed_child_result_is_rejected(self):
        body = ('import pathlib,sys\n'
                'p=pathlib.Path(sys.argv[sys.argv.index("--result-file")+1])\n'
                'p.write_text("not json")\n')
        with self._fake_cli(body):
            with self.assertRaisesRegex(RuntimeError, 'malformed result'):
                refresh.run_collect(self.db, self.projects, config_path=self.config,
                                    timeout=10, env=self.env)

    def test_inline_refresh_keeps_custom_alert_callback_contract(self):
        db_path = self.root / 'inline.sqlite'
        observed = []

        def send_alerts(db, config, on_alerts=None):
            self.assertEqual({'scope': 'embedded'}, config)
            if on_alerts is not None:
                on_alerts([{'id': 'alert'}])
            return {'sent': 0}

        with patch.dict(os.environ, {'HOME': str(self.home),
                                     'CODE_LOOP_HOME': str(self.home / '.code-loop')}), \
                patch.object(serve.collect, 'DEFAULT_REGISTRY',
                             self.home / '.code-loop' / 'registry.sqlite'), \
                patch.object(serve.collect, 'sweep_agent_runs', return_value=None), \
                patch.object(serve.notify, 'push_new_alerts', side_effect=send_alerts):
            inline = serve.make_refresh(
                db_path, root=self.projects, config={'scope': 'embedded'},
                on_alerts=lambda fresh: observed.extend(fresh))
            result = inline()
        self.assertEqual([{'id': 'alert'}], observed)
        self.assertEqual({'projects': 0, 'failed': []}, result['collect'])

    def test_inline_refresh_returns_skipped_when_formal_round_owns_lock(self):
        db_path = self.root / 'busy.sqlite'
        lock = DatabaseRoundLock(db_path)
        self.assertTrue(lock.acquire())
        try:
            with patch.object(serve.collect, 'collect',
                              side_effect=AssertionError('collect should not run')):
                result = serve.make_refresh(db_path, root=self.projects)()
        finally:
            lock.close()
        self.assertEqual('collect in progress', result['collect']['skipped'])


if __name__ == '__main__':
    unittest.main()
