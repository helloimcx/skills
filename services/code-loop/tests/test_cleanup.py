#!/usr/bin/env python3
"""Tests for the gate-parallel teardown guard (issue #216).

The guard has two parts: ``conftest.py`` wraps every ``TemporaryDirectory``
cleanup in this suite, and ``_cleanup.retry_cleanup`` is the bounded retry.
These tests pin the retry mechanics — a transient writer is retried with
evidence, a genuinely stuck directory still raises — and the suite-wide
installation of the wrapper.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import _cleanup
from _cleanup import retry_cleanup
try:
    import conftest
except ImportError:
    pass

# Captured at import time, before any test monkeypatches it: the true
# original cleanup (conftest.py already wrapped the class attribute).
_TRUE_ORIG = _cleanup._ORIG_CLEANUP


class _FakeTmp:
    name = 'fake/tmp'


def _flaky_cleanup(failures):
    """Return (cleanup, calls): cleanup fails the first ``failures`` calls."""
    calls = []

    def cleanup(tmp):
        calls.append(1)
        if len(calls) <= failures:
            raise OSError(145, '目录不是空的')
    return cleanup, calls


class CleanupRetryTests(unittest.TestCase):
    def tearDown(self):
        _cleanup._ORIG_CLEANUP = _TRUE_ORIG

    def test_succeeds_immediately_when_no_writer(self):
        cleanup, calls = _flaky_cleanup(0)
        _cleanup._ORIG_CLEANUP = cleanup
        retry_cleanup(_FakeTmp(), retries=3, delay=0.01)
        self.assertEqual(len(calls), 1)

    def test_retries_until_transient_writer_finishes(self):
        cleanup, calls = _flaky_cleanup(2)
        _cleanup._ORIG_CLEANUP = cleanup
        buf = io.StringIO()
        with redirect_stdout(buf):
            retry_cleanup(_FakeTmp(), retries=5, delay=0.01)
        self.assertEqual(len(calls), 3)
        # Evidence of the transient writer: the WARN names the retry count.
        self.assertIn('WARN cleanup needed 2 retry(ies)', buf.getvalue())

    def test_raises_after_exhausting_retries(self):
        cleanup, calls = _flaky_cleanup(99)
        _cleanup._ORIG_CLEANUP = cleanup
        with self.assertRaises(OSError):
            retry_cleanup(_FakeTmp(), retries=3, delay=0.01)
        self.assertEqual(len(calls), 3)

    def test_conftest_wraps_cleanup_suite_wide(self):
        self.assertEqual(tempfile.TemporaryDirectory.cleanup.__name__,
                         '_retrying_cleanup')

    def test_patched_cleanup_still_removes_real_dir(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, 'x.txt').write_text('hi')
        self.assertFalse(Path(td).exists())


if __name__ == '__main__':
    unittest.main()