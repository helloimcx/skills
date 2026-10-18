"""Gate-parallel teardown guard (issue #216).

``verify_skill.py`` runs each test file as its own pytest process, up to 8 in
parallel (ThreadPoolExecutor). On Windows a freshly-written temp dir can still
be receiving file-handle flushes — sqlite ``-wal``/``-shm`` checkpoint,
git worktree metadata, killed subprocess handles — when ``tearDown`` calls
``TemporaryDirectory.cleanup()``; ``shutil.rmtree`` then fails with
WinError 145 (ERROR_DIR_NOT_EMPTY). The observed hits were in a different
file each gate run, so the guard is applied here at the suite root instead of
per test: every ``TemporaryDirectory`` in this suite gets the bounded retry
(see ``_cleanup.retry_cleanup``).  The retry is bounded and still raises on a
genuinely stuck directory, with the residue listed as writer evidence — this
is not ``ignore_cleanup_errors``.
"""
import tempfile

from _cleanup import retry_cleanup


def _retrying_cleanup(self):
    retry_cleanup(self)


tempfile.TemporaryDirectory.cleanup = _retrying_cleanup