"""Collector tests: project discovery, Gitea snapshot diffing, CP runtime copy.

Uses a loopback fake Gitea (no production network) and a fixture control-plane
SQLite shaped like the real one.
"""
import gc
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import collect, ingest, store


def issue(n, **extra):
    base = dict(number=n, state='open', title='Issue %d' % n, body='',
                labels=[{'name': 'x'}], created_at='2026-09-01T00:00:00Z',
                updated_at='2026-09-01T00:00:00Z', closed_at=None,
                html_url='http://g/%d' % n, comments=[])
    base.update(extra)
    return base


def pr(n, **extra):
    base = dict(number=n, state='open', title='PR %d' % n, body='Closes #%d' % (n - 100),
                labels=[], created_at='2026-09-02T00:00:00Z',
                updated_at='2026-09-02T00:00:00Z', closed_at=None,
                html_url='http://g/pr/%d' % n,
                head={'sha': 's%d' % n, 'ref': 'feature/x'},
                base={'sha': 'base', 'ref': 'main'}, merged=False,
                comments=[], reviews=[])
    base.update(extra)
    return base


CP_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE work_items (id INTEGER PRIMARY KEY, key TEXT, kind TEXT, version INTEGER,
  payload TEXT, resources TEXT, state TEXT, priority INTEGER, created_at TEXT);
CREATE TABLE executions (id INTEGER PRIMARY KEY, item_id INTEGER, worker TEXT, token TEXT,
  expires_at TEXT, state TEXT, worktree TEXT, branch TEXT, head TEXT, result TEXT,
  item_version INTEGER, created_at TEXT);
CREATE TABLE leases (resource TEXT, execution_id INTEGER, token TEXT, expires_at TEXT);
CREATE TABLE outbox (id INTEGER PRIMARY KEY, execution_id INTEGER, action TEXT,
  payload TEXT, state TEXT, receipt TEXT);
CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, created_at TEXT);
"""


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # The observer store is machine-global; keep each case hermetic.
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.root = Path(self.tmp.name) / 'projects'
        self.db_path = Path(self.tmp.name) / 'dash.sqlite'
        self.routes = {}

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                path = url.path.replace('/api/v1/repos/a/b', '').replace('/repos/a/b', '')
                page = int(parse_qs(url.query).get('page', ['0'])[0])
                outer.hits.append((path, page))
                response = outer.routes.get((path, page), [])
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_port
        self._dbs = []
        self.hits = []
        self.seed_routes()
        self.project = self.make_project('a__b', enabled=True, cp=True)

    def tearDown(self):
        for db in getattr(self, '_dbs', []):
            db.close()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def expire_observer(self):
        """Age the shared store so the next collect crawls — the test
        equivalent of the observation TTL elapsing."""
        db = sqlite3.connect(str(Path(os.environ['CODE_LOOP_HOME']) / 'observer.sqlite'))
        db.execute("UPDATE meta SET value=? WHERE key='synced_at'", (json.dumps(0),))
        db.commit()
        db.close()

    def seed_routes(self):
        self.routes[('/branches/main', 0)] = {'commit': {'id': 'base'}}
        self.routes[('/issues', 1)] = [
            issue(1, comments=[{'id': 1, 'body': 'c1', 'user': {'login': 'u'},
                                'created_at': '2026-09-01T05:00:00Z'}]),
            issue(2, state='closed', closed_at='2026-09-01T00:00:00Z')]
        self.routes[('/issues/1/comments', 1)] = [
            {'id': 1, 'body': 'c1', 'user': {'login': 'u'}, 'created_at': '2026-09-01T05:00:00Z'}]
        full_pr = pr(101, comments=[
            {'id': 9, 'body': 'Approved for manual merge', 'user': {'login': 'rev'},
             'created_at': '2026-09-03T00:00:00Z'}])
        self.routes[('/pulls', 1)] = [pr(101)]
        self.routes[('/pulls/101', 0)] = full_pr
        self.routes[('/issues/101/comments', 1)] = full_pr['comments']
        self.routes[('/pulls/101/reviews', 1)] = []

    def make_project(self, name, enabled, cp):
        project_dir = self.root / name
        (project_dir / 'state').mkdir(parents=True)
        config = {'schema_version': 2,
                  'platform': {'url': 'http://127.0.0.1:%d' % self.port, 'repo': 'a/b',
                               'token_env': 'GITEA_TOKEN', 'timeout': 5,
                               'page_limit': 50, 'max_pages': 50},
                  'base': 'main'}
        (project_dir / 'control-plane.json').write_text(json.dumps(config), encoding='utf-8')
        if enabled:
            (project_dir / 'state' / 'machine-enabled.json').write_text(
                json.dumps({'host': 'test-host', 'enabled_at': 1780000000.0}), encoding='utf-8')
        if cp:
            src = sqlite3.connect(str(project_dir / 'state' / 'control-plane.sqlite'))
            src.executescript(CP_SCHEMA)
            src.execute("INSERT INTO work_items VALUES (1,'k','discover',1,'{}','{}','DONE',1,"
                        "'2026-09-05T00:00:00Z')")
            src.execute("INSERT INTO executions VALUES (1,1,'w','','','FINISHED','wt','br','h',"
                        "NULL,1,'2026-09-05T01:00:00Z')")
            src.execute("INSERT INTO events VALUES (1,'machine.enabled','{}','2026-09-05T00:00:00Z')")
            src.commit()
            src.close()
        return project_dir

    def connect(self):
        db = store.connect(self.db_path)
        self._dbs.append(db)
        return db

    def test_collect_config_matches_the_cp_crawl_config(self):
        # Both crawlers feed one store, so both must fetch the same facts.
        # Compare every fact-bearing key the Gitea adapter reads: a drift here
        # would flip the stored snapshot between two shapes (work-item churn).
        from codeloop.cp.runtime import _load_coordinator_config
        config_path = self.project / 'control-plane.json'
        raw = json.loads(config_path.read_text(encoding='utf-8'))
        raw.update({'required_checks': ['ci/build'], 'base': 'main',
                    'platform': dict(raw['platform'], ca_file='gitea-ca.pem',
                                     page_limit=25, max_pages=50)})
        config_path.write_text(json.dumps(raw), encoding='utf-8')
        (self.project / 'gitea-ca.pem').write_text('x', encoding='utf-8')
        dash_cfg, base = collect.load_config(config_path)
        cp_cfg = _load_coordinator_config(json.loads(json.dumps(raw)), self.project)['platform']
        # token_file is excluded by design: the CP passes the credential path
        # explicitly while the adapter defaults to the very same file — the
        # resolved token is identical either way.
        facts = ('url', 'repo', 'token_env', 'ca_file', 'base',
                 'required_checks', 'page_limit', 'max_pages')
        for key in facts:
            if key == 'ca_file':
                # Same file wins regardless of Windows 8.3/long-form spelling.
                self.assertTrue(os.path.samefile(cp_cfg[key], dash_cfg[key]), key)
            else:
                self.assertEqual(cp_cfg.get(key), dash_cfg.get(key), key)
        self.assertEqual('main', base)

    def test_discover_projects_sorted(self):
        self.make_project('b__c', enabled=False, cp=False)
        found = collect.discover_projects(self.root)
        self.assertEqual([self.root / 'a__b' / 'control-plane.json',
                          self.root / 'b__c' / 'control-plane.json'], found)

    def test_collect_project_populates_store(self):
        db = self.connect()
        result = collect.collect_project(self.db_path, self.project / 'control-plane.json')
        self.assertTrue(result['ok'])
        project = db.execute('SELECT * FROM projects').fetchone()
        self.assertEqual('a__b', project['org_repo'])
        self.assertEqual(1, project['enabled'])
        self.assertEqual('test-host', project['enabled_host'])
        self.assertEqual(1780000000.0, project['enabled_at'])
        self.assertIsNone(project['last_error'])
        self.assertIsNotNone(project['last_ok_collect_at'])
        self.assertEqual(2, db.execute('SELECT COUNT(*) FROM issues').fetchone()[0])
        self.assertEqual(1, db.execute('SELECT COUNT(*) FROM prs').fetchone()[0])
        verdict = store.latest_verdict(db, project['id'], 101)
        self.assertEqual('Approved for manual merge', verdict[0])
        items = db.execute('SELECT * FROM work_items').fetchall()
        self.assertEqual(1, len(items))
        self.assertEqual('discover', items[0]['kind'])
        self.assertEqual(1, db.execute('SELECT COUNT(*) FROM cp_events').fetchone()[0])
        self.assertEqual(1, db.execute('SELECT COUNT(*) FROM sync_log WHERE ok=1').fetchone()[0])

    def test_second_collect_reads_the_observer_store_without_http(self):
        db = self.connect()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        self.hits.clear()
        stats = collect.collect_project(self.db_path, self.project / 'control-plane.json')['stats']
        self.assertEqual([], self.hits, 'a fresh store must be served without any crawl')
        self.assertEqual(2, stats['issues_unchanged'])

    def test_collect_project_idempotent(self):
        db = self.connect()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        stats = collect.collect_project(self.db_path, self.project / 'control-plane.json')['stats']
        self.assertEqual(2, stats['issues_unchanged'])
        self.assertEqual(1, stats['prs_unchanged'])
        self.assertEqual(0, stats['issues_added'] + stats['issues_updated']
                         + stats['prs_added'] + stats['prs_updated'])

    def test_snapshot_change_is_reflected(self):
        db = self.connect()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        self.routes[('/pulls/101', 0)] = pr(101, state='closed', merged=True,
                                            merged_at='2026-09-04T00:00:00Z')
        self.expire_observer()
        stats = collect.collect_project(self.db_path, self.project / 'control-plane.json')['stats']
        self.assertEqual(1, stats['prs_updated'])
        row = db.execute('SELECT * FROM prs').fetchone()
        self.assertEqual(1, row['merged'])

    def test_collect_all_isolates_failures(self):
        broken = self.make_project('z__w', enabled=False, cp=False)
        config = json.loads((broken / 'control-plane.json').read_text(encoding='utf-8'))
        config['platform']['url'] = 'http://127.0.0.1:1'
        config['platform']['repo'] = 'z/w'
        (broken / 'control-plane.json').write_text(json.dumps(config), encoding='utf-8')
        db = self.connect()
        summary = collect.collect(db, root=self.root)
        self.assertEqual(2, summary['projects'])
        self.assertEqual(1, len(summary['failed']))
        broken_row = db.execute("SELECT * FROM projects WHERE org_repo='z__w'").fetchone()
        self.assertIsNotNone(broken_row['last_error'])
        good_row = db.execute("SELECT * FROM projects WHERE org_repo='a__b'").fetchone()
        self.assertIsNotNone(good_row['last_ok_collect_at'])

    def test_disabled_marker_absent(self):
        (self.project / 'state' / 'machine-enabled.json').unlink()
        db = self.connect()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        self.assertEqual(0, db.execute('SELECT enabled FROM projects').fetchone()[0])

    def test_snapshot_uses_configured_base_branch(self):
        # A project whose base branch is not main must snapshot its own base,
        # not the adapter default.
        config = json.loads((self.project / 'control-plane.json').read_text(encoding='utf-8'))
        config['base'] = 'master'
        (self.project / 'control-plane.json').write_text(json.dumps(config), encoding='utf-8')
        self.routes[('/branches/master', 0)] = {'commit': {'id': 'm1'}}
        db = self.connect()
        result = collect.collect_project(self.db_path, self.project / 'control-plane.json')
        self.assertTrue(result['ok'])
        self.assertIn(('/branches/master', 0), self.hits)
        self.assertNotIn(('/branches/main', 0), self.hits)

    def test_locked_store_fails_one_project_not_the_run(self):
        """2026-09-14 starvation: a busy-store OperationalError during the
        first project's bookkeeping must fail that project only — the run
        continues, the failure is recorded, later projects still collect."""
        self.make_project('m__n', enabled=False, cp=False)  # a__b < m__n
        db = self.connect()
        original = store.upsert_project

        def busy(db_, org_repo, *args, **kwargs):
            if org_repo == 'a__b':
                raise sqlite3.OperationalError('database is locked')
            return original(db_, org_repo, *args, **kwargs)

        store.upsert_project = busy
        try:
            summary = collect.collect(db, root=self.root)
        finally:
            store.upsert_project = original
        self.assertEqual(2, summary['projects'])
        self.assertEqual(1, len(summary['failed']))
        self.assertIn('database is locked', summary['failed'][0])
        good = db.execute("SELECT last_ok_collect_at FROM projects "
                          "WHERE org_repo='m__n'").fetchone()
        self.assertIsNotNone(good['last_ok_collect_at'])

    def test_collect_never_skips_and_reruns_are_idempotent(self):
        """2026-09-23 permanent stall: a leaked filesystem lock held by a live
        process made every later collection skip forever — the stale-timeout
        takeover never fired because the holder never died. With the lock gone
        a second run always proceeds, and the store stays consistent (no
        duplicated rows) because every write is an idempotent upsert."""
        db = self.connect()
        first = collect.collect(db, root=self.root)
        second = collect.collect(db, root=self.root)
        self.assertNotIn('skipped', first)
        self.assertNotIn('skipped', second)
        self.assertEqual([], first['failed'])
        self.assertEqual([], second['failed'])
        dupes = db.execute(
            'SELECT COUNT(*) FROM (SELECT project_id, number FROM prs '
            'GROUP BY project_id, number HAVING COUNT(*) > 1)').fetchone()[0]
        self.assertEqual(0, dupes, '重跑不得产生重复 PR 行')
        db.close()

    def test_upsert_fact_concurrent_insert_keeps_one_row(self):
        """No lock: two collectors can ingest the same fresh row at once. The
        atomic INSERT ... ON CONFLICT must turn the race into a no-op (one
        inserts, the other updates) — never a UNIQUE violation, never two
        rows. Each thread owns its connection, mirroring collect_project."""
        setup = self.connect()
        pid = store.upsert_project(setup, 'a__b')
        setup.close()
        row = {'state': 'open', 'title': 'racing', 'body': '', 'head_sha': 's',
               'head_ref': 'feature/x', 'base_ref': 'main', 'merged': 0,
               'merged_at': None, 'mergeable': 1, 'issue_numbers': '[]',
               'created_at': '2026-09-01T00:00:00Z',
               'updated_at': '2026-09-01T00:00:00Z',
               'closed_at': None, 'html_url': 'http://g/9'}
        barrier = threading.Barrier(2)
        real_hash = ingest._hash

        def synced_hash(value):
            digest = real_hash(value)
            barrier.wait(timeout=10)  # both threads reach the SELECT with no row yet
            return digest

        errors, outcomes = [], []

        def race():
            db = store.connect(self.db_path)
            try:
                outcomes.append(ingest._upsert_fact(db, 'prs', pid, 999, row))
            except Exception as exc:  # the UNIQUE-collision regression under test
                errors.append(exc)
            finally:
                db.close()

        ingest._hash = synced_hash
        try:
            threads = [threading.Thread(target=race) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
        finally:
            ingest._hash = real_hash
        self.assertEqual([], errors, '并发 upsert 不得抛异常（含 UNIQUE 冲突）')
        self.assertEqual(2, len(outcomes))
        db = self.connect()
        count = db.execute('SELECT COUNT(*) FROM prs WHERE project_id=? AND number=999',
                           (pid,)).fetchone()[0]
        self.assertEqual(1, count, '并发插入同一新行只能落一行')
        db.close()

    def test_second_collect_skips_unchanged_closed_threads(self):
        db = self.connect()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        self.hits.clear()
        # Age the store so the collector's own crawl runs again: the stored
        # facts are the incremental basis the adapter skips threads with.
        self.expire_observer()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        paths = [p for p, _ in self.hits]
        self.assertNotIn('/issues/2/comments', paths)      # closed & unchanged
        self.assertIn('/issues/1/comments', paths)         # open -> always refreshed
        self.assertIn(('/pulls', 1), self.hits)            # listings always run

    def test_closed_pr_threads_fetched_when_dashboard_store_empty(self):
        """issue #165 回归：observer store 已含 closed+unchanged PR（**空线程**），
        dashboard store 为空（库重置/首部署）→ collect 必须全量抓该 PR 的评论/评审，
        不得以空线程落库且永不自愈。

        reviewer MAJOR-1 修正：预填 observer 必须补 base_sha meta（快照可服务）、
        closed PR 用空 comments/reviews（复现"空线程"场景）、mock /pulls 返回
        closed+unchanged PR（thread_stable 会跳过）——无修复时服务空线程快照断言
        失败，有修复时 known={} 触发全量爬通过。
        """
        # 预填 observer store：closed+unchanged PR（空线程），模拟 #164 后
        # observer 已抓过、跳过线程抓取的状态。
        obs = sqlite3.connect(str(Path(os.environ['CODE_LOOP_HOME']) / 'observer.sqlite'))
        obs.executescript('''
            CREATE TABLE IF NOT EXISTS items (
                project_key TEXT NOT NULL, kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
                number INTEGER, state TEXT, updated_at TEXT, raw_json TEXT NOT NULL,
                PRIMARY KEY (project_key, kind, number));
            CREATE TABLE IF NOT EXISTS meta (
                project_key TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY (project_key, key));
        ''')
        # 快照可服务：base_sha + synced_at 都在 TTL 内。
        obs.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)",
                    ('a__b', 'base_sha', json.dumps('base')))
        obs.execute("INSERT OR REPLACE INTO meta VALUES (?,?,?)",
                    ('a__b', 'synced_at', json.dumps(time.time())))
        # closed+unchanged PR，空 comments/reviews（observer 跳过线程抓取的真实形状）。
        closed_pr = pr(101, state='closed', closed_at='2026-09-02T01:00:00Z',
                       updated_at='2026-09-02T01:00:00Z',
                       comments=[], reviews=[])
        obs.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?)",
                    ('a__b', 'pr', 0, 101, 'closed', '2026-09-02T01:00:00Z',
                     json.dumps(closed_pr)))
        obs.commit()
        obs.close()

        # mock /pulls 返回 closed+unchanged PR（thread_stable 会跳过线程抓取），
        # 但 /pulls/101/comments 与 /pulls/101/reviews 返回真实评论——修复后
        # known={} 触发全量爬会抓取它们。
        self.routes[('/pulls', 1)] = [pr(101, state='closed',
                                         closed_at='2026-09-02T01:00:00Z',
                                         updated_at='2026-09-02T01:00:00Z')]
        self.routes[('/issues/101/comments', 1)] = [
            {'id': 9, 'body': 'Approved for manual merge', 'user': {'login': 'rev'},
             'created_at': '2026-09-03T00:00:00Z'}]
        self.routes[('/pulls/101/reviews', 1)] = [
            {'id': 1, 'user': {'login': 'rev'}, 'state': 'approved',
             'body': 'LGTM', 'submitted_at': '2026-09-03T00:00:00Z'}]

        # 空 dashboard store 跑 collect：observer 快照在 TTL 内可服务（空线程），
        # 但 dashboard 的 known 为空 → _covers 失败 → 必须全量抓该 closed PR 的评论/评审。
        db = self.connect()
        self.hits.clear()
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        pid = db.execute("SELECT id FROM projects WHERE org_repo='a__b'").fetchone()['id']
        comments = db.execute("SELECT COUNT(*) AS n FROM comments WHERE project_id=? AND kind='pr' AND number=101",
                              (pid,)).fetchone()['n']
        self.assertEqual(comments, 1, 'closed PR 评论必须以完整线程落库（issue #165）')
        reviews = db.execute("SELECT COUNT(*) AS n FROM pr_reviews WHERE project_id=? AND pr_number=101",
                             (pid,)).fetchone()['n']
        self.assertEqual(reviews, 1, 'closed PR 评审必须以完整线程落库（issue #165）')
        # 后续轮次（observer 快照未过期）不重复抓取，但评论仍在。
        collect.collect_project(self.db_path, self.project / 'control-plane.json')
        comments2 = db.execute("SELECT COUNT(*) AS n FROM comments WHERE project_id=? AND kind='pr' AND number=101",
                               (pid,)).fetchone()['n']
        self.assertEqual(comments2, 1)

    def test_only_filter(self):
        self.make_project('b__c', enabled=False, cp=False)
        db = self.connect()
        summary = collect.collect(db, root=self.root, only={'b__c'})
        self.assertEqual(1, summary['projects'])
        self.assertEqual(0, len(summary['failed']))
        self.assertIsNone(db.execute("SELECT * FROM projects WHERE org_repo='a__b'").fetchone())
        self.assertIsNotNone(db.execute("SELECT * FROM projects WHERE org_repo='b__c'").fetchone())

    def test_collect_parallel_collects_all_projects_and_isolates_failures(self):
        """issue #160 回归：项目级并发采集（1A 2B）。

        ① 并发路径存在：collect 必须经 ThreadPoolExecutor 并行提交项目
        （回退串行循环时本测试变红）；② 多项目全部落库、单个失败隔离——
        与串行语义一致，并发不破坏失败隔离。"""
        from concurrent.futures import ThreadPoolExecutor
        broken = self.make_project('z__w', enabled=False, cp=False)
        config = json.loads((broken / 'control-plane.json').read_text(encoding='utf-8'))
        config['platform']['url'] = 'http://127.0.0.1:1'
        config['platform']['repo'] = 'z/w'
        (broken / 'control-plane.json').write_text(json.dumps(config), encoding='utf-8')
        db = self.connect()
        spawned = []
        original_executor = collect.ThreadPoolExecutor

        def spy_executor(max_workers=None, **kwargs):
            spawned.append(max_workers)
            return original_executor(max_workers=max_workers, **kwargs)

        collect.ThreadPoolExecutor = spy_executor
        try:
            summary = collect.collect(db, root=self.root, max_workers=3)
        finally:
            collect.ThreadPoolExecutor = original_executor
        self.assertEqual([3], spawned, 'collect 必须用 ThreadPoolExecutor(max_workers) 并发采集')
        self.assertEqual(2, summary['projects'])
        self.assertEqual(1, len(summary['failed']))
        self.assertIn('z__w', summary['failed'][0])
        # a__b 并发采集成功落库
        good_row = db.execute("SELECT * FROM projects WHERE org_repo='a__b'").fetchone()
        self.assertIsNotNone(good_row)
        self.assertIsNotNone(good_row['last_ok_collect_at'])
        # z__w 失败但被隔离：有记录（last_error），不阻塞其余项目
        broken_row = db.execute("SELECT * FROM projects WHERE org_repo='z__w'").fetchone()
        self.assertIsNotNone(broken_row['last_error'])

    def test_collect_parallel_workers_use_independent_connections(self):
        """PR #180 review MAJOR-1 回归：并发 worker 不得共享 sqlite3 连接。

        共享连接跨线程时，一个 worker 的事务回滚会静默丢弃另一个 worker
        的未提交写入（事务交错）。修复后每个 worker 经 store.connect(db_path)
        自建独立连接——spy store.connect 断言并发路径为每个项目各开一个连接，
        且共享的调用方连接不被传入线程（collect_project 收 db_path 而非 db）。"""
        from concurrent.futures import ThreadPoolExecutor
        broken = self.make_project('z__w', enabled=False, cp=False)
        config = json.loads((broken / 'control-plane.json').read_text(encoding='utf-8'))
        config['platform']['url'] = 'http://127.0.0.1:1'
        config['platform']['repo'] = 'z/w'
        (broken / 'control-plane.json').write_text(json.dumps(config), encoding='utf-8')
        db = self.connect()
        connected = []
        original_connect = store.connect

        def spy_connect(path, *args, **kwargs):
            connected.append(str(path))
            return original_connect(path, *args, **kwargs)

        store.connect = spy_connect
        try:
            summary = collect.collect(db, root=self.root, max_workers=3)
        finally:
            store.connect = original_connect
        # 每个项目（a__b、z__w）各开一次独立连接；collect_machines 无 registry
        # 文件不触发额外连接。共享的 db 连接绝不能被 collect_project 使用。
        self.assertEqual(2, summary['projects'])
        self.assertEqual(1, len(summary['failed']))
        self.assertEqual(2, len(connected), '并发 worker 必须每项目独立连接，而非共享调用方 db')
        self.assertTrue(all(Path(self.db_path).resolve() == Path(p).resolve() for p in connected),
                        'worker 连接必须指向同一 store 文件: %s' % connected)
        # 数据仍落库（独立连接写入对调用方连接可见——同一文件、WAL）
        good_row = db.execute("SELECT * FROM projects WHERE org_repo='a__b'").fetchone()
        self.assertIsNotNone(good_row)
        self.assertIsNotNone(good_row['last_ok_collect_at'])


class MachineRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(str(Path(self.tmp.name) / 'dash.sqlite'))

    def tearDown(self):
        self.db.close()
        if getattr(self, '_reg', None) is not None:
            self._reg.close()
        gc.collect()
        self.tmp.cleanup()

    def _registry(self, path):
        from codeloop.cp.registry import Registry
        self._reg = Registry(str(path))
        return self._reg

    def test_collect_machines_snapshots_registry(self):
        reg = self._registry(Path(self.tmp.name) / 'registry.sqlite')
        code = reg.create_enroll_code('dev-b', ['a__b'])
        out = reg.enroll(code, agent_version='9', addr='10.0.0.8')
        reg.touch(out['machine_id'], addr='10.0.0.8', agent_version='9')
        registry_path = Path(self.tmp.name) / 'registry.sqlite'
        machines = collect.collect_machines(self.db, registry_path)
        self.assertEqual(len(machines), 1)
        self.assertEqual(machines[0]['name'], 'dev-b')
        self.assertEqual(machines[0]['projects'], 'a__b')
        overview = store.overview(self.db)
        self.assertEqual(len(overview['machines']), 1)
        self.assertEqual(overview['machines'][0]['name'], 'dev-b')
        self.assertEqual(overview['machines'][0]['agent_version'], '9')

    def test_collect_machines_missing_registry_is_noop(self):
        self.assertIsNone(collect.collect_machines(
            self.db, Path(self.tmp.name) / 'absent.sqlite'))
        self.assertEqual(store.overview(self.db)['machines'], [])

    def test_collect_reports_registry_failure_in_isolation(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / 'not-a-db.sqlite'
            bad.write_text('garbage')
            with self.assertRaises(sqlite3.Error):
                collect.collect_machines(self.db, bad)

    def test_cli_collect_survives_broken_operator_store(self):
        """MINOR-3: dashboard.py collect must not die when the operator store
        is corrupt — open_store raising inside the guard settles the
        subscription to None (like cp_server) and the collect carries on."""
        import io
        import unittest.mock as mock
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = home / 'dashboard.json'
            # operator config enabled → the CLI tries to open the broken store
            config.write_text(json.dumps({'operator': {'enabled': True}}),
                              encoding='utf-8')

            def broken_open(*a, **kw):
                raise sqlite3.DatabaseError('file is not a database')

            import dashboard as dash_cli
            with mock.patch.object(dash_cli, 'DEFAULT_CONFIG', str(config)), \
                    mock.patch.object(dash_cli.operator_store, 'open_store',
                                      side_effect=broken_open), \
                    mock.patch.object(dash_cli.collect, 'collect',
                                      return_value={'failed': 0, 'done': []}), \
                    mock.patch.object(dash_cli.notify, 'push_new_alerts',
                                      return_value={}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = dash_cli.main(['collect', '--db', str(home / 'dash.sqlite'),
                                        '--root', str(home / 'projects')])
                self.assertEqual(rc, 0)
                self.assertIn('"operator_store": "unavailable"', buf.getvalue())
                self.assertIn('not a database', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
