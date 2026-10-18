"""Frontend asset contract: the dash UI lives in services/code-loop/frontend/
(shell + app.css + app.js + vendored libs) and serve.py exposes it through a
fixed whitelist. Guards: the three shell aliases return the shell verbatim,
every /static/* referenced by the shell is whitelisted and every whitelist
entry is referenced (a reference without an entry is a silent 404 in the
browser; a dead entry is an unreachable route), unlisted paths stay 404, and
all frontend JS passes node --check when node is on PATH — the repo gate does
not parse JS, and a stray `};` once shipped in a PR
(docs/plans/2026-09-18-agent-runner-plugins.md)."""
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop.dash import serve, store


def _serve(httpd):
    """issue #216/#199: teardown must be shutdown -> join -> server_close."""
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(httpd, thread=None):
    httpd.shutdown()
    if thread is not None:
        thread.join(timeout=10)
    httpd.server_close()


class FrontendServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'dash.sqlite')
        handler = serve.create(self.db)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._thread = _serve(self.server)
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        _stop(self.server, self._thread)
        self.db.close()
        self.tmp.cleanup()

    def get(self, path):
        request = Request(self.base + path)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_shell_aliases_serve_index_html(self):
        shell = (serve.APP_HTML).read_bytes()
        for path in ('/', '/index.html', '/app.html'):
            status, headers, body = self.get(path)
            self.assertEqual(200, status, path)
            self.assertTrue(
                headers.get('Content-Type', '').startswith('text/html'), path)
            self.assertEqual(shell, body, path)

    def test_whitelisted_assets_served_with_mime(self):
        # 整个映射逐字锁死（URL→文件名→MIME）：期望值不取自 serve.py，
        # 否则映射写错（如 app.js 指到 marked.min.js）时测试与实现同源恒真。
        expected = {'/static/app.css': ('app.css', 'text/css'),
                    '/static/app.js': ('app.js', 'application/javascript'),
                    '/static/marked.min.js': ('vendor/marked.min.js', 'application/javascript'),
                    '/static/purify.min.js': ('vendor/purify.min.js', 'application/javascript')}
        self.assertEqual(expected, serve.STATIC_FILES)
        for path, (name, mime) in expected.items():
            status, headers, body = self.get(path)
            self.assertEqual(200, status, path)
            self.assertTrue(headers.get('Content-Type', '').startswith(mime), path)
            self.assertEqual((serve.STATIC_DIR / name).read_bytes(), body, path)

    def test_unlisted_static_path_is_404(self):
        status, _, body = self.get('/static/serve.py')
        self.assertEqual(404, status)
        self.assertIn(b'not found', body)


class FrontendReferenceGuardTests(unittest.TestCase):
    """Filesystem-level drift guard, no server needed."""

    def test_shell_references_and_whitelist_agree(self):
        html = serve.APP_HTML.read_text(encoding='utf-8')
        refs = set(re.findall(r'(?:src|href)="/static/([^"]+)"', html))
        names = {url[len('/static/'):] for url in serve.STATIC_FILES}
        self.assertEqual(names, refs,
                         'shell references and STATIC_FILES whitelist drifted')
        for url, (name, _) in serve.STATIC_FILES.items():
            path = serve.STATIC_DIR / name
            self.assertTrue(path.is_file(), 'whitelisted asset missing: %s' % path)

    def test_shell_references_local_assets_only(self):
        html = serve.APP_HTML.read_text(encoding='utf-8')
        for url in re.findall(r'<(?:script|link)[^>]+?(?:src|href)="([^"]+)"', html):
            self.assertTrue(url.startswith('/static/') or url.startswith('http'),
                            'unexpected asset reference: %s' % url)


class FrontendJsSyntaxTests(unittest.TestCase):
    def test_node_check_passes_on_all_frontend_js(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node not on PATH')
        js_files = [serve.STATIC_DIR / name
                    for url, (name, mime) in serve.STATIC_FILES.items()
                    if mime == 'application/javascript']
        for js in js_files:
            with self.subTest(js=str(js)):
                proc = subprocess.run([node, '--check', str(js)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(0, proc.returncode, proc.stderr)

    def test_projects_of_runtime_smoke(self):
        """issue #334 repair: node --check 拦不住运行时 TypeError——projectsOf
        曾误传机器对象 m（m.split 不存在）致 renderMachines 整体崩溃。提取
        projectsOf 定义做运行时冒烟（字符串 m.projects、空串、null 均返回
        字符串不抛错）。基线同步后 projectsOf 逐项过 displayProject（issue
        #325 项目名三写法统一），一并抽出该定义锁定两者共用同一套展示语义，
        而非只验一层手写桩。"""
        node = shutil.which('node')
        if not node:
            self.skipTest('node not on PATH')
        js = serve.STATIC_DIR / 'app.js'
        script = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const d = src.match(/const displayProject = p => \{[\s\S]*?\n\};\r?\n/);
const m = src.match(/const projectsOf = raw =>[\s\S]*?\|\| '[\s\S]';\r?\n/);
if (!d || !m) { console.error('displayProject/projectsOf not found'); process.exit(2); }
// eval 内 const 不泄漏到外层作用域：把定义与断言放进同一函数体执行
const DATA = {ov: {projects: [{org_repo: 'mocx__skills', repo: 'mocx/skills'}]}};
const body = d[0] + m[0] + `
const cases = [
  ['yilian-yilian_server_ai__ai_memory,yilian-yilian_server_ai__ai_vector',
   'yilian-yilian_server_ai/ai_memory、yilian-yilian_server_ai/ai_vector'],
  ['', '无'],
  [null, '无'],
  ['mocx__skills', 'mocx/skills'],
  ['D:/ai/skills, yilian-yilian_server_ai__ai_memory',
   'skills（本地项目）、yilian-yilian_server_ai/ai_memory'],
];
for (const [input, want] of cases) {
  const got = projectsOf(input);
  if (got !== want) { console.error('projectsOf(%j) = %j, want %j', input, got, want); process.exit(1); }
}
console.log('projectsOf smoke OK');
`;
new Function('DATA', body)(DATA);
'''
        proc = subprocess.run([node, '-e', script, str(js)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, proc.returncode, proc.stderr)


if __name__ == '__main__':
    unittest.main()
