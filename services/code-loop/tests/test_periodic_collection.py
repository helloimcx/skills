import importlib.util
import os
import tempfile
import threading
import time
import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock

SERVICE = Path(__file__).resolve().parents[1]
CHILD_ENV = dict(os.environ, PYTHONPATH=str(SERVICE) + os.pathsep + os.environ.get('PYTHONPATH', ''))
spec = importlib.util.spec_from_file_location('periodic_cp_server', SERVICE / 'scripts' / 'cp_server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

from codeloop.dash import collect, serve, store


class PeriodicCollectionTests(unittest.TestCase):
    def test_timer_retries_after_failure_and_stops(self):
        calls = []
        stop = threading.Event()
        def submit(_):
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise RuntimeError('temporary failure')
            if len(calls) == 3:
                stop.set()
        thread = threading.Thread(target=server._collection_driver, args=(submit, 0.02, stop))
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(3, len(calls))

    def test_periodic_and_manual_share_single_flight(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = store.connect(str(Path(tmp) / 'dashboard.sqlite'))
            started, release = threading.Event(), threading.Event()
            def collect(_):
                started.set()
                release.wait(2)
            handler = serve.create(db, on_collect=collect)
            try:
                self.assertTrue(handler.submit_collection(None)['collect']['started'])
                self.assertTrue(started.wait(1))
                self.assertEqual('collect in progress', handler.submit_collection(None)['collect']['skipped'])
                handler.close_collection()
                self.assertEqual('server shutting down', handler.submit_collection(None)['collect']['skipped'])
            finally:
                release.set()
                handler.wait_for_collection(2)
                db.close()

    def test_collection_period_validation(self):
        for bad in (-1, True, '30'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / 'server.json').write_text(__import__('json').dumps({'collection_period': bad}))
                with self.assertRaises(SystemExit):
                    server.load_settings(tmp)

    def test_failed_project_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            refresh = serve.make_refresh(str(Path(tmp) / 'dashboard.sqlite'))
            with mock.patch.object(serve.collect, 'collect', return_value={'projects': 1, 'failed': ['project failed']}), \
                 mock.patch.object(serve.notify, 'push_new_alerts', return_value={'error': None}):
                with mock.patch.object(serve.sys, 'stderr') as error:
                    refresh()
                    self.assertIn('project failed', ''.join(str(c) for c in error.write.call_args_list))

    def test_collection_lock_serializes_threads_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'dashboard.sqlite')
            entered = threading.Event()
            def contender():
                with collect.collection_lock(path):
                    entered.set()
            with collect.collection_lock(path):
                thread = threading.Thread(target=contender)
                thread.start()
                self.assertFalse(entered.wait(0.05))
            self.assertTrue(entered.wait(1))
            thread.join(1)

    def test_busy_collection_fails_bounded_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'dashboard.sqlite')
            ready, release = threading.Event(), threading.Event()
            def holder():
                with collect.collection_lock(path):
                    ready.set()
                    release.wait(2)
            thread = threading.Thread(target=holder)
            thread.start()
            try:
                self.assertTrue(ready.wait(1))
                with self.assertRaisesRegex(collect.CollectError, 'already in progress'):
                    with collect.collection_lock(path, timeout=0.05):
                        pass
            finally:
                release.set()
                thread.join(2)
            with collect.collection_lock(path, timeout=0.05):
                pass

    def test_headless_handler_uses_same_collection_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'dashboard.sqlite'
            calls = []
            with mock.patch.dict(os.environ, {'CODE_LOOP_HOME': tmp}), \
                 mock.patch.object(server, '_dash_config', return_value=None), \
                 mock.patch.object(serve, 'make_refresh', return_value=lambda only: calls.append(only)):
                handler, db, op = server._headless_collection_handler({'projects_root': tmp}, mock.Mock())
                try:
                    handler.submit_collection(None)
                    self.assertTrue(handler.wait_for_collection(2))
                    self.assertEqual([None], calls)
                    self.assertIsNone(op)
                    self.assertTrue(db_path.exists())
                finally:
                    db.close()

    def test_collection_lock_serializes_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'dashboard.sqlite')
            marker = Path(tmp) / 'entered'
            code = ('from codeloop.dash.collect import collection_lock\n'
                    'from pathlib import Path\n'
                    'import sys\n'
                    'with collection_lock(sys.argv[1]): Path(sys.argv[2]).touch()\n')
            with collect.collection_lock(path):
                proc = subprocess.Popen([sys.executable, '-c', code, path, str(marker)], env=CHILD_ENV)
                try:
                    time.sleep(0.2)
                    self.assertIsNone(proc.poll())
                    self.assertFalse(marker.exists())
                finally:
                    pass
            self.assertEqual(0, proc.wait(timeout=3))
            self.assertTrue(marker.exists())

    def test_stuck_project_does_not_keep_process_alive(self):
        code = ('import threading,time\n'
                'from codeloop.dash import collect\n'
                'collect.collect_project = lambda *args: time.sleep(20)\n'
                't=threading.Thread(target=collect._collect_projects, args=("db", ["p"], 1), daemon=True)\n'
                't.start(); time.sleep(0.2)\n')
        proc = subprocess.Popen([sys.executable, '-c', code], env=CHILD_ENV)
        try:
            self.assertEqual(0, proc.wait(timeout=3))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_data_home_isolation_applies_to_dashboard_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(CHILD_ENV, CODE_LOOP_HOME=tmp)
            code = ('from codeloop.dash import collect\n'
                    'print(collect.DEFAULT_DB)\n'
                    'print(collect.DEFAULT_ROOT)\n'
                    'print(collect.DEFAULT_REGISTRY)\n')
            out = subprocess.check_output([sys.executable, '-c', code], env=env, text=True)
            self.assertEqual([str(Path(tmp) / 'dashboard.sqlite'), str(Path(tmp) / 'projects'),
                              str(Path(tmp) / 'server' / 'registry.sqlite')], out.splitlines())

    def test_partial_worker_start_failure_drains_started_workers(self):
        original = threading.Thread.start
        starts = []
        processed = []
        def start(thread):
            starts.append(thread)
            if len(starts) == 2:
                raise RuntimeError('no threads')
            return original(thread)
        with mock.patch.object(threading.Thread, 'start', start), \
             mock.patch.object(collect, 'collect_project', side_effect=lambda db, config: processed.append(config)):
            with self.assertRaisesRegex(RuntimeError, 'no threads'):
                collect._collect_projects('db', ['a', 'b'], 2)
        self.assertCountEqual(['a', 'b'], processed)
