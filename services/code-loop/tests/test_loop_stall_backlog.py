"""issue #300 loop_stall 空队列误报：handler/reviewer 空队列空闲（claim null =
合规空闲）不得报停摆，只有 READY 积压存在时才报。discover 是调度驱动，
裸静默仍报。每个用例钉住 issue 验收的一条判据。"""
import sys as _sys
import tempfile
import unittest
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from _dashboard_store_base import StoreTestCase, epoch, ts
from codeloop.dash import store


def runtime(kind, last_day, last_hour=0, ready_day=None, ready_hour=0):
    """work_items: 一个 DONE（记录最后活动）+ 可选 READY 积压。"""
    items = [{'id': 1, 'key': 'k', 'kind': kind, 'state': 'DONE',
              'priority': None, 'created_at': ts(last_day, last_hour),
              'payload': '{}', 'version': 1, 'resources': '{}'}]
    if ready_day is not None:
        items.append({'id': 2, 'key': 'k2', 'kind': kind, 'state': 'READY',
                      'priority': None, 'created_at': ts(ready_day, ready_hour),
                      'payload': '{}', 'version': 1, 'resources': '{}'})
    return {
        'work_items': items,
        'executions': [{'id': 1, 'item_id': 1, 'worker': 'w', 'state': 'FINISHED',
                        'result': None, 'worktree': '', 'branch': '', 'head': '',
                        'expires_at': None, 'created_at': ts(last_day, last_hour)}],
        'leases': [], 'outbox': [], 'events': []}


class LoopStallBacklogTests(StoreTestCase):
    def stall_rules(self, kind, **runtime_kw):
        pid = self.pid()
        store.replace_runtime(self.db, pid, runtime(kind, **runtime_kw))
        alerts = store.alerts(self.db, now=self.now, config={})
        return {a['rule'] for a in alerts}

    def test_handler_empty_queue_no_stall(self):
        """空队列 + last activity 超龄：handler 不产 loop_stall（claim null 合规空闲）。"""
        rules = self.stall_rules('handler', last_day=18)
        self.assertNotIn('loop_stall', rules)

    def test_reviewer_empty_queue_no_stall(self):
        """同 handler：reviewer 空队列超龄不报。"""
        rules = self.stall_rules('reviewer', last_day=18)
        self.assertNotIn('loop_stall', rules)

    def test_handler_with_ready_backlog_still_stalls(self):
        """有 READY 积压且超龄 → 仍报 loop_stall。"""
        rules = self.stall_rules('handler', last_day=18, ready_day=19)
        self.assertIn('loop_stall', rules)

    def test_discover_empty_queue_still_stalls(self):
        """discover 是调度驱动，空队列裸静默仍报（回归保护）。"""
        rules = self.stall_rules('discover', last_day=18)
        self.assertIn('loop_stall', rules)

    def test_fresh_handler_no_stall(self):
        """活动未超龄 → 不报（既有行为回归）。"""
        rules = self.stall_rules('handler', last_day=20, last_hour=10)
        self.assertNotIn('loop_stall', rules)


if __name__ == '__main__':
    unittest.main()