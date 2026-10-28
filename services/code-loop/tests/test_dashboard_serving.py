"""Persistent dashboard serving model: bytes, revision fence and dependencies."""
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / 'scripts')):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import serving, store
from codeloop.dash import collect
from codeloop.dash.schema import SERVING_SOURCE_TABLES


class ServingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / 'dashboard.sqlite'
        self.db = store.connect(self.db_path)
        self.pid = store.upsert_project(self.db, 'a__b', 'config.json')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_precompute_matches_independent_baseline_payloads_and_gzip(self):
        summary = serving.precompute(self.db_path)
        self.assertEqual(3, summary['built'])
        expected = {
            'items': {'issues': [], 'prs': []},
            'operator': {'ok': True, 'total': 0, 'rows': []},
            'runtime:"a__b"': {
                'work_items': [], 'executions': [], 'leases': [],
                'outbox': [], 'cp_events': []},
        }
        for key, payload in expected.items():
            cached = serving.lookup(self.db, key)
            self.assertIsNotNone(cached, key)
            raw, compressed = cached
            self.assertEqual(payload, json.loads(raw.decode('utf-8')), key)
            self.assertEqual(raw, gzip.decompress(compressed), key)

    def test_populated_domain_matches_fixed_pre_optimization_payloads(self):
        self.db.execute('''INSERT INTO issues
            (id,project_id,number,state,title,body,labels,created_at,updated_at,
             closed_at,html_url,content_hash)
            VALUES (10,1,4,'closed','Issue four','issue body','[]','created',
                    'updated','closed','https://g/issues/4','issue-hash')''')
        self.db.execute('''INSERT INTO prs
            (id,project_id,number,state,title,body,head_sha,head_ref,base_ref,
             merged,merged_at,mergeable,issue_numbers,created_at,updated_at,
             closed_at,html_url,content_hash)
            VALUES (20,1,9,'open','PR nine','pr body','sha','feature','main',
                    0,NULL,NULL,'[]','created','updated',NULL,
                    'https://g/pulls/9','pr-hash')''')
        payload = '{"pr":{"number":9}}'
        result = ('{"decision":"Approved for manual merge","reviewer":"agent",'
                  '"summary":"looks good"}')
        self.db.execute('''INSERT INTO work_items
            (id,project_id,cp_id,key,kind,state,priority,version,payload,resources,
             created_at,content_hash)
            VALUES (30,1,50,'review:9','reviewer','DONE',3,1,?,'[]','item-time','wi-hash')''',
            (payload,))
        self.db.execute('''INSERT INTO executions
            (id,project_id,cp_id,item_cp_id,worker,state,result,worktree,branch,
             head,expires_at,created_at,content_hash)
            VALUES (40,1,60,50,'box/reviewer','SUCCEEDED',?,'wt','main','sha',
                    NULL,'1780000000','exec-hash')''', (result,))
        self.db.execute('''INSERT INTO leases
            (id,project_id,resource,execution_cp_id,token,expires_at,content_hash)
            VALUES (50,1,'project/a',60,'lease-token','later','lease-hash')''')
        self.db.execute('''INSERT INTO outbox
            (id,project_id,cp_id,execution_cp_id,action,state,receipt,created_at,content_hash)
            VALUES (60,1,70,60,'publish','SENT','receipt','out-time','out-hash')''')
        self.db.execute('''INSERT INTO cp_events
            (id,project_id,cp_id,kind,payload,created_at,content_hash)
            VALUES (70,1,80,'claim','{}','event-time','event-hash')''')
        signal_payload = '{"severity":"warn","message":"high cpu","note":"check"}'
        self.db.execute('''INSERT INTO operator_signals
            (id,source,kind,key,org_repo,severity,message,payload,state,created_at)
            VALUES ('signal-1','local','cpu','hot','a__b','warn','high cpu',?,'OPEN',10)''',
            (signal_payload,))
        run_result = ('{"outcome":"fixed","diagnosis":"cool down",'
                      '"actions":["limit"],"evidence":["sample"]}')
        self.db.execute('''INSERT INTO operator_runs
            (id,signal_id,worker,state,result,agent,model,started_at,ended_at)
            VALUES ('run-1','signal-1','box','SUCCEEDED',?,'agent','model',12,13)''',
            (run_result,))

        expected = {
            'items': {
                'issues': [{'id': 10, 'project_id': 1, 'number': 4, 'state': 'closed',
                            'title': 'Issue four', 'body': 'issue body', 'labels': '[]',
                            'created_at': 'created', 'updated_at': 'updated',
                            'closed_at': 'closed', 'html_url': 'https://g/issues/4',
                            'content_hash': 'issue-hash', 'org_repo': 'a__b'}],
                'prs': [{'id': 20, 'project_id': 1, 'number': 9, 'state': 'open',
                         'title': 'PR nine', 'body': 'pr body', 'head_sha': 'sha',
                         'head_ref': 'feature', 'base_ref': 'main', 'merged': 0,
                         'merged_at': None, 'mergeable': None, 'issue_numbers': '[]',
                         'created_at': 'created', 'updated_at': 'updated', 'closed_at': None,
                         'html_url': 'https://g/pulls/9', 'content_hash': 'pr-hash',
                         'org_repo': 'a__b', 'verdict': 'Approved for manual merge',
                         'verdict_at': 1780000000.0}],
            },
            'operator': {'ok': True, 'total': 1, 'rows': [{
                'id': 'signal-1',
                'signal': {'source': 'local', 'kind': 'cpu', 'key': 'hot',
                           'org_repo': 'a__b', 'severity': 'warn',
                           'message': 'high cpu', 'note': 'check', 'received_at': 10.0},
                'state': 'OPEN', 'rounds': [{
                    'id': 'run-1', 'signal_id': 'signal-1', 'worker': 'box',
                    'state': 'SUCCEEDED', 'result': run_result, 'agent': 'agent',
                    'model': 'model', 'started_at': 12.0, 'ended_at': 13.0}],
                'agent': 'agent', 'model': 'model', 'started_at': 12.0,
                'ended_at': 13.0, 'run_state': 'SUCCEEDED', 'outcome': 'fixed',
                'diagnosis': 'cool down', 'actions': ['limit'], 'evidence': ['sample'],
            }]},
            'runtime:"a__b"': {
                'work_items': [{'id': 30, 'project_id': 1, 'cp_id': 50,
                                'key': 'review:9', 'kind': 'reviewer', 'state': 'DONE',
                                'priority': 3, 'version': 1, 'payload': payload,
                                'resources': '[]', 'created_at': 'item-time'}],
                'executions': [{'id': 40, 'project_id': 1, 'cp_id': 60,
                                'item_cp_id': 50, 'worker': 'box/reviewer',
                                'state': 'SUCCEEDED', 'result': result,
                                'worktree': 'wt', 'branch': 'main', 'head': 'sha',
                                'expires_at': None, 'created_at': '1780000000'}],
                'leases': [{'id': 50, 'project_id': 1, 'resource': 'project/a',
                            'execution_cp_id': 60, 'token': 'lease-token',
                            'expires_at': 'later'}],
                'outbox': [{'id': 60, 'project_id': 1, 'cp_id': 70,
                            'execution_cp_id': 60, 'action': 'publish', 'state': 'SENT',
                            'receipt': 'receipt', 'created_at': 'out-time'}],
                'cp_events': [{'id': 70, 'project_id': 1, 'cp_id': 80,
                               'kind': 'claim', 'payload': '{}',
                               'created_at': 'event-time'}],
            },
        }
        serving.precompute(self.db_path)
        for key, payload in expected.items():
            cached = serving.lookup(self.db, key)
            self.assertIsNotNone(cached, key)
            self.assertEqual(payload, json.loads(cached[0].decode('utf-8')), key)

    def test_every_declared_source_write_invalidates_the_cache(self):
        expected = {'projects', 'issues', 'prs', 'comments', 'work_items',
                    'executions', 'leases', 'outbox', 'cp_events',
                    'operator_signals', 'operator_runs'}
        self.assertEqual(expected, set(SERVING_SOURCE_TABLES))
        self.db.executescript("""
            INSERT INTO issues(project_id,number,state) VALUES (1,1,'open');
            INSERT INTO prs(project_id,number,state) VALUES (1,2,'open');
            INSERT INTO comments(project_id,kind,number) VALUES (1,'pr',2);
            INSERT INTO work_items(project_id,cp_id) VALUES (1,1);
            INSERT INTO executions(project_id,cp_id) VALUES (1,1);
            INSERT INTO leases(project_id,resource) VALUES (1,'r');
            INSERT INTO outbox(project_id,cp_id) VALUES (1,1);
            INSERT INTO cp_events(project_id,cp_id) VALUES (1,1);
            INSERT INTO operator_signals(id,source,kind,key,payload,state,created_at)
              VALUES ('s','test','k','x','{}','OPEN',1);
            INSERT INTO operator_runs(id,signal_id,worker,state,started_at)
              VALUES ('r','s','w','DONE',1);
        """)
        for table in SERVING_SOURCE_TABLES:
            row = self.db.execute('SELECT rowid AS rid FROM %s LIMIT 1' % table).fetchone()
            self.assertIsNotNone(row, table)
            before = serving.source_revision(self.db)
            column = 'org_repo' if table == 'projects' else 'rowid'
            self.db.execute('UPDATE %s SET %s=%s WHERE rowid=?' %
                            (table, column, column),
                            (row['rid'],))
            self.assertGreater(serving.source_revision(self.db), before, table)

    def test_irrelevant_project_timestamp_does_not_invalidate_pure_mirror_cache(self):
        before = serving.source_revision(self.db)
        self.db.execute('UPDATE projects SET last_collect_at=123 WHERE id=?',
                        (self.pid,))
        self.assertEqual(before, serving.source_revision(self.db))
        self.db.execute("UPDATE projects SET org_repo='renamed__project' WHERE id=?",
                        (self.pid,))
        self.assertGreater(serving.source_revision(self.db), before)

    def test_cache_writes_do_not_advance_source_revision(self):
        before = serving.source_revision(self.db)
        self.db.execute("INSERT INTO serving_cache VALUES ('scratch',x'01',x'02',0,1)")
        self.db.execute("UPDATE serving_cache SET source_revision=1 WHERE key='scratch'")
        self.db.execute("DELETE FROM serving_cache WHERE key='scratch'")
        self.assertEqual(before, serving.source_revision(self.db))

    def test_source_change_during_build_rejects_publication(self):
        def race(_revision):
            self.db.execute("UPDATE projects SET org_repo='moved__during_build' WHERE id=?",
                            (self.pid,))

        result = serving.precompute(self.db_path, before_publish=race)
        self.assertEqual('source changed', result['discarded'])
        self.assertIsNone(serving.lookup(self.db, 'items'))

    def test_identical_json_reuses_existing_large_blobs(self):
        serving.precompute(self.db_path)
        self.db.execute("CREATE TRIGGER no_serving_blob_rewrite "
                        "BEFORE UPDATE OF body,gzip_body ON serving_cache "
                        "BEGIN SELECT RAISE(ABORT,'blob rewrite'); END")
        # This source field advances the durable revision but does not affect
        # any of the three response payloads.
        self.db.execute("UPDATE projects SET last_error='new round' WHERE id=?",
                        (self.pid,))
        self.assertEqual(3, serving.precompute(self.db_path)['built'])

    def test_only_exact_default_urls_receive_canonical_cache_keys(self):
        self.assertEqual('items', serving.key_for_request(
            self.db, '/api/items', {}, ''))
        self.assertIsNone(serving.key_for_request(
            self.db, '/api/items', {'fresh': '1'}, 'fresh=1'))
        self.assertIsNone(serving.key_for_request(
            self.db, '/api/items', {'unknown': '1'}, 'unknown=1'))
        self.assertEqual('runtime:"a__b"', serving.key_for_request(
            self.db, '/api/runtime', {'project': 'a__b'}, 'project=a__b'))
        self.assertIsNone(serving.key_for_request(
            self.db, '/api/runtime', {'project': 'a__b', 'extra': '1'},
            'project=a__b&extra=1'))
        self.assertIsNone(serving.key_for_request(
            self.db, '/api/runtime', {'project': 'missing'}, 'project=missing'))

    def test_copy_runtime_reads_all_source_tables_from_one_cp_snapshot(self):
        cp_path = Path(self.tmp.name) / 'control-plane.sqlite'
        source = sqlite3.connect(str(cp_path))
        source.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE work_items(id INTEGER PRIMARY KEY,state TEXT);
            CREATE TABLE executions(id INTEGER PRIMARY KEY,result TEXT);
            INSERT INTO work_items VALUES (1,'OLD');
            INSERT INTO executions VALUES (1,'old-result');
        """)
        source.close()
        real_connect = sqlite3.connect
        mutated = []

        class SourceProxy:
            def __init__(self, connection):
                self.connection = connection

            @property
            def row_factory(self):
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self.connection.row_factory = value

            def execute(self, sql, params=()):
                cursor = self.connection.execute(sql, params)
                if sql == 'SELECT * FROM work_items' and not mutated:
                    writer = real_connect(str(cp_path))
                    writer.execute("UPDATE work_items SET state='NEW'")
                    writer.execute("UPDATE executions SET result='new-result'")
                    writer.commit()
                    writer.close()
                    mutated.append(True)
                return cursor

            def commit(self):
                return self.connection.commit()

            def close(self):
                return self.connection.close()

        with patch.object(collect.sqlite3, 'connect',
                          side_effect=lambda *a, **kw: SourceProxy(real_connect(*a, **kw))):
            collect.copy_runtime(self.db, self.pid, cp_path)
        self.assertTrue(mutated)
        self.assertEqual('OLD', self.db.execute(
            'SELECT state FROM work_items WHERE project_id=?', (self.pid,)).fetchone()[0])
        self.assertEqual('old-result', self.db.execute(
            'SELECT result FROM executions WHERE project_id=?',
            (self.pid,)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
