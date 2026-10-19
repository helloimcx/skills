#!/usr/bin/env python3
"""测试拉起的 serve 必须与启动方同生共死，且绝不占看板端口（issue #249）。

2026-09-21 现场：机器上累积 35 个 temp-home 测试 cp_server，其中一个在 8712
空窗期抢占了看板端口，导致生产守护 UI 绑定失败、看板 HTTP 000。两条根因：

1. CLI 测试拉 serve 时只写 projects_root，temp home 继承 ``init`` 默认
   ``ui_port: 8712`` —— 只要该端口短暂空闲就被测试 server 绑走并长占。
2. 父 pytest 被门禁挤压超时**硬杀**时 finally 不执行，teardown 的
   terminate/kill 全部落空，子进程变孤儿长驻。

对应两道防线：测试侧一律 ``ui_port: null``（UI 监听器根本不启动），以及
serve 的 opt-in ``--exit-on-stdin-eof``（启动方管道关闭即自关）。本文件锁定
这两条不变量本身，而不是依赖某个具体测试的写法。
"""
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'cp_server.py'


def free_port():
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def wait_health(port, deadline=15.0):
    end = time.time() + deadline
    while time.time() < end:
        try:
            with urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def read_banner(stream, deadline=10.0):
    """First stdout line that parses as the startup JSON envelope.

    serve 先打人类可读日志（如陈旧锁迁移提示）再打 JSON banner，且行数不固定，
    所以按内容找而不是按位置读；读线程 + 超时保证管道无新行时不会挂住测试。
    """
    lines = queue.Queue()

    def pump():
        for line in stream:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    end = time.time() + deadline
    while time.time() < end:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and 'result' in payload:
            return payload
    return None


class ServeSpawnHygieneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'server'
        self.env = dict(os.environ, CODE_LOOP_HOME=str(Path(self.tmp.name) / 'data'),
                        PYTHONIOENCODING='utf-8')
        self.servers = []
        run = subprocess.run([sys.executable, str(SCRIPT), '--home', str(self.home), 'init'],
                             capture_output=True, text=True, encoding='utf-8',
                             errors='replace', cwd=str(REPO))
        self.assertEqual(0, run.returncode, run.stderr)
        settings = json.loads((self.home / 'server.json').read_text(encoding='utf-8'))
        settings['projects_root'] = str(Path(self.tmp.name) / 'projects')
        settings['ui_port'] = None
        (self.home / 'server.json').write_text(json.dumps(settings), encoding='utf-8')

    def tearDown(self):
        for serve in self.servers:
            if serve.poll() is None:
                serve.kill()
                try:
                    serve.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self.tmp.cleanup()

    def spawn(self, port):
        serve = subprocess.Popen(
            [sys.executable, str(SCRIPT), '--home', str(self.home), 'serve',
             '--port', str(port), '--exit-on-stdin-eof'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', cwd=str(REPO), env=self.env)
        self.servers.append(serve)
        return serve

    def test_test_serve_never_binds_the_operator_ui_port(self):
        """ui_port: null ⇒ UI 监听器根本不启动，启动 banner 自报 ui=None。

        banner 的 ``ui: None`` 覆盖全部"未绑定 UI"路径（ui_port 为 null、
        非 loopback host、端口被占、bind 失败），共同不变量是**没有 bind**
        ——issue #249 的抢占路径就此关闭。"""
        port = free_port()
        serve = self.spawn(port)
        self.assertTrue(wait_health(port), 'serve did not come up')
        banner = read_banner(serve.stdout)
        self.assertIsNotNone(banner, 'no JSON startup banner on stdout')
        self.assertIsNone(banner['result']['ui'],
                          'test serve must not bind the operator UI port')

    def test_exit_on_stdin_eof_requires_a_pipe(self):
        """无管道却传 flag ⇒ 立即响亮拒绝，绝不「报成功再静默自杀」。

        反例（review 复现）：stdin=DEVNULL 时 watcher 立刻读到 EOF，serve 打印
        ``ok: true`` 后 rc=0 退出、端口从未可用——症状与它要防的泄漏一模一样，
        且 supervisor 看不出异常。"""
        port = free_port()
        run = subprocess.run(
            [sys.executable, str(SCRIPT), '--home', str(self.home), 'serve',
             '--port', str(port), '--exit-on-stdin-eof'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding='utf-8', errors='replace', cwd=str(REPO), env=self.env)
        self.assertNotEqual(0, run.returncode)
        self.assertIn('EXIT_ON_STDIN_EOF_REQUIRES_PIPE', run.stdout + run.stderr)
        self.assertNotIn('"ok": true', run.stdout)  # 不得先报成功

    def test_every_spawn_site_is_guarded(self):
        """元测试：spawn cp_server serve 的测试文件必须同时关 UI 端口 + 挂自关。

        两道防线都是调用点约定，容易在新增测试时漏掉——漏了就是 8712 抢占与
        孤儿的回归（#249 的根因正是「两个 CLI 测试没关 UI 端口」）。"""
        tests_dir = Path(__file__).resolve().parent
        spawn_sites = []
        for path in sorted(tests_dir.glob('test_*.py')):
            text = path.read_text(encoding='utf-8')
            if "'cp_server.py'" not in text or "'serve'" not in text:
                continue
            spawn_sites.append(path.name)
            self.assertIn("ui_port'] = None", text,
                          '%s spawns cp_server serve without disabling the UI port' % path.name)
            self.assertIn('--exit-on-stdin-eof', text,
                          '%s spawns cp_server serve without the launcher-death guard' % path.name)
        self.assertIn('test_cp_server.py', spawn_sites,
                      'spawn-site scan found nothing — the detector itself is broken')

    def test_serve_exits_when_launcher_dies(self):
        """启动方被硬杀（无 teardown、无 terminate/kill）时，serve 必须在
        stdin EOF 后自行退出——这是孤儿的机制性防线，不依赖 finally 执行。"""
        port = free_port()
        serve = self.spawn(port)
        self.assertTrue(wait_health(port), 'serve did not come up')
        # 硬杀的等价信号：启动方进程消失 ⇒ 其管道写端被 OS 关闭 ⇒ 子进程读到 EOF
        serve.stdin.close()
        try:
            serve.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.fail('serve outlived its launcher: no EOF self-shutdown')
        self.assertIsNotNone(serve.returncode)
        # 端口随之释放：不再有服务应答（替代进程可重新绑定）
        deadline = time.time() + 5.0
        bound = False
        while time.time() < deadline:
            probe = socket.socket()
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(('127.0.0.1', port))
                bound = True
                break
            except OSError:
                time.sleep(0.1)
            finally:
                probe.close()
        if not bound:
            self.fail('port not released after self-shutdown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
