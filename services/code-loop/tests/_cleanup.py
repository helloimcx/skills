"""Windows-safe TemporaryDirectory cleanup for gate-parallel flakes.

issue #216：门禁并行（verify_skill.py ThreadPoolExecutor 并发跑 49 个 pytest
进程）时偶发 WinError 145（ERROR_DIR_NOT_EMPTY）——tearDown 的
``TemporaryDirectory.cleanup()`` 与目录内写入方（sqlite -wal/-shm 落盘、
git worktree 元数据、被 kill 的子进程句柄）收尾竞争。单独跑无并发负载时
写入方早已退出，故永不复现；高负载下收尾被拉长，窗口才稳定出现。

重试退避是 Windows 常规做法：cleanup 失败说明目录内还有写入方，短等后
重试（写入方退出后 rmtree 自然成功），不是 ``ignore_cleanup_errors`` 式
静默吞掉——重试仍失败会原样抛出，且每次失败会打印目录残留内容作为
「谁还在写」的诊断证据；重试成功也会打 WARN 记录需要几次才收尾。

``_ORIG_CLEANUP`` 在 import 时捕获原始实现：``conftest.py`` 会把
``TemporaryDirectory.cleanup`` 全局包上重试，这里必须绕开包装层，否则
``retry_cleanup`` 里的 ``tmp.cleanup()`` 会递归进包装后的版本。
"""
import tempfile
import time
from pathlib import Path

DEFAULT_RETRIES = 5
DEFAULT_DELAY = 0.3

_ORIG_CLEANUP = tempfile.TemporaryDirectory.cleanup


def retry_cleanup(tmp, retries: int = DEFAULT_RETRIES,
                  delay: float = DEFAULT_DELAY) -> None:
    """Clean up a TemporaryDirectory, retrying on transient OSError.

    On failure the directory's remaining entries are printed (diagnostic for
    the writer that is still alive); after ``retries`` attempts the original
    error is re-raised so real resource leaks stay loud. A cleanup that only
    succeeds after retries prints a WARN with the retry count — the gate's
    evidence that the transient writer existed.
    """
    for attempt in range(retries):
        try:
            _ORIG_CLEANUP(tmp)
            if attempt:
                print('WARN cleanup needed %d retry(ies) for %s'
                      % (attempt, getattr(tmp, 'name', '?')))
            return
        except OSError as exc:
            if attempt == retries - 1:
                _dump_residue(getattr(tmp, 'name', ''), exc)
                raise
            _dump_residue(getattr(tmp, 'name', ''), exc)
            time.sleep(delay * (attempt + 1))


def _dump_residue(name, exc) -> None:
    try:
        entries = sorted(str(p.relative_to(name)) for p in Path(name).rglob('*'))
    except OSError:
        entries = ['<unreadable>']
    print('WARN cleanup retry (%s): %s; residue: %s'
          % (name, exc, ', '.join(entries[:10]) or '<empty>'))
