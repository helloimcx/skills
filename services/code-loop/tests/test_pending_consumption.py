"""pending_consumption tests (issue #94): the handler-side arrival signal.

AC1: three tagged issues — user replied / agent spoke last / no comments —
and the deterministic list contains exactly the first one.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import store

import pending_consumption


def ts(day, hour=0, minute=0):
    return '2026-09-%02dT%02d:%02d:00Z' % (day, hour, minute)


def gitea_issue(n, **extra):
    base = dict(number=n, state='open', title='[❓needs-human] 决策 %d' % n, body='',
                labels=[], created_at=ts(1), updated_at=ts(1), closed_at=None,
                html_url='http://g/issue/%d' % n, comments=[])
    base.update(extra)
    return base


def comment(body, day, hour=0, poster='mocx'):
    return {'id': body, 'body': body, 'user': {'login': poster},
            'created_at': ts(day, hour)}


def agent_comment(body, day, hour=0, role='handler'):
    c = comment(body, day, hour, poster='mocx')
    c['body'] = body + '\n\n<!-- code-loop-agent: %s -->' % role
    return c


class PendingConsumptionTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'dash.sqlite'
        self.db = store.connect(self.path)
        self.pid = store.upsert_project(self.db, 'mocx__skills', 'cp.json',
                                        platform_url='http://g', repo='mocx/skills')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def load(self, issues):
        store.apply_snapshot(self.db, self.pid,
                             {'issues': list(issues), 'prs': [], 'base_sha': 'b'})

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = pending_consumption.main(['--db', str(self.path), *argv])
        return rc, out.getvalue(), err.getvalue()


class ArrivalDetectionTests(PendingConsumptionTestCase):
    def test_ac1_exactly_the_user_replied_issue(self):
        """AC1：用户已回复 → 命中；agent 最后发言 / 无评论 → 不命中。"""
        self.load([
            gitea_issue(1, comments=[agent_comment('决策清单 🙋', 8, 10),
                                     comment('B', 9, 11)]),
            gitea_issue(2, comments=[comment('1C', 8, 10),
                                     agent_comment('已收到，转开发', 9, 11)]),
            gitea_issue(3),
        ])
        rows = store.pending_consumption(self.db)
        self.assertEqual([1], [r['number'] for r in rows])

    def test_cli_reports_only_the_pending_issue(self):
        self.load([
            gitea_issue(1, comments=[agent_comment('决策清单 🙋', 8, 10),
                                     comment('B', 9, 11)]),
            gitea_issue(2, comments=[agent_comment('决策清单 🙋', 9, 11)]),
            gitea_issue(3),
        ])
        rc, out, _ = self.run_cli('--project', 'mocx__skills')
        self.assertEqual(0, rc)
        self.assertIn('mocx__skills#1', out)
        self.assertNotIn('#2', out)
        self.assertNotIn('#3', out)
        self.assertIn('1 条', out)

    def test_empty_list_is_not_an_error(self):
        self.load([gitea_issue(3)])
        rc, out, _ = self.run_cli()
        self.assertEqual(0, rc)
        self.assertIn('0 条', out)

    def test_untagged_issue_never_listed(self):
        """未打标 = 不是决策类，用户评论不构成待消费决策。"""
        self.load([gitea_issue(4, title='[BUG] 普通缺陷', comments=[comment('看下', 9, 11)])])
        self.assertEqual([], store.pending_consumption(self.db))

    def test_closed_issue_never_listed(self):
        self.load([gitea_issue(5, state='closed', comments=[comment('B', 9, 11)])])
        self.assertEqual([], store.pending_consumption(self.db))

    def test_label_tagged_issue_listed(self):
        """打标走 label（非标题前缀）同样命中。"""
        self.load([gitea_issue(6, title='决策类', labels=[{'name': 'needs-human'}],
                               comments=[comment('B', 9, 11)])])
        self.assertEqual([6], [r['number'] for r in store.pending_consumption(self.db)])

    def test_dashboard_proxy_reply_counts(self):
        """dashboard 代理回复（user_replies）与平台评论同权。"""
        self.load([gitea_issue(7, comments=[agent_comment('决策清单 🙋', 8, 10)])])
        store.record_user_reply(self.db, self.pid, 7, '1A', ts(9, 11))
        self.assertEqual([7], [r['number'] for r in store.pending_consumption(self.db)])

    def test_agent_reply_after_user_reply_not_pending(self):
        """用户回复后 agent 已澄清 → 球在用户一侧，不算待消费。"""
        self.load([gitea_issue(8, comments=[comment('B', 9, 10),
                                            agent_comment('请确认 B 的范围', 9, 11)])])
        self.assertEqual([], store.pending_consumption(self.db))

    def test_legacy_agent_marker_not_a_user_reply(self):
        """迁移前的 opcode 标记（<!-- code-loop:... -->）仍识别为 agent 发言。"""
        self.load([gitea_issue(9, comments=[
            comment('B', 9, 10),
            comment('已处理\n\n<!-- code-loop:abc123 -->', 9, 11)])])
        self.assertEqual([], store.pending_consumption(self.db))

    def test_decision_anchor_alone_is_agent_comment(self):
        """无机器标记但含「决策清单」锚点的历史 agent 评论不算用户回复。"""
        self.load([gitea_issue(10, comments=[
            comment('B', 9, 10),
            comment('**决策清单 🙋**\n\n1. 选 A 还是 B', 9, 11)])])
        self.assertEqual([], store.pending_consumption(self.db))

    def test_oldest_reply_first(self):
        """按等待时长排序：最早的用户回复排最前。"""
        self.load([
            gitea_issue(11, comments=[comment('B', 9, 12)]),
            gitea_issue(12, comments=[comment('A', 8, 12)]),
        ])
        rows = store.pending_consumption(self.db, now=store.parse_ts(ts(10)))
        self.assertEqual([12, 11], [r['number'] for r in rows])
        self.assertGreater(rows[0]['waiting_seconds'], rows[1]['waiting_seconds'])


class CliContractTests(PendingConsumptionTestCase):
    def test_missing_store_exits_2(self):
        rc, _, err = self.run_cli('--db', str(Path(self.tmp.name) / 'nope.sqlite'))
        self.assertEqual(2, rc)
        self.assertIn('不存在', err)

    def test_unknown_project_exits_2(self):
        rc, _, err = self.run_cli('--project', 'no__such')
        self.assertEqual(2, rc)
        self.assertIn('项目不存在', err)

    def test_project_filter_scopes_output(self):
        other = store.upsert_project(self.db, 'other__repo', 'cp2.json')
        store.apply_snapshot(self.db, self.pid, {'issues': [
            gitea_issue(1, comments=[comment('B', 9, 11)])], 'prs': [], 'base_sha': 'b'})
        store.apply_snapshot(self.db, other, {'issues': [
            gitea_issue(2, comments=[comment('B', 9, 11)])], 'prs': [], 'base_sha': 'b'})
        rc, out, _ = self.run_cli('--project', 'mocx__skills')
        self.assertEqual(0, rc)
        self.assertIn('mocx__skills#1', out)
        self.assertNotIn('other__repo#2', out)

    def test_json_output_shape(self):
        self.load([gitea_issue(1, comments=[comment('B', 9, 11)])])
        rc, out, _ = self.run_cli('--json')
        self.assertEqual(0, rc)
        import json
        payload = json.loads(out)
        self.assertEqual(1, len(payload['pending']))
        self.assertEqual(1, payload['pending'][0]['number'])
        self.assertEqual('mocx__skills', payload['pending'][0]['org_repo'])


if __name__ == '__main__':
    unittest.main(verbosity=2)