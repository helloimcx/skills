"""Stable per-dashboard collect-round kernel lock.

The file itself is intentionally never unlinked: unlinking a held lock could
let a third process lock a new inode while the first owner still runs.
"""
import os
from pathlib import Path


class DatabaseRoundLock:
    def __init__(self, db_path):
        db_path = os.fspath(db_path)
        # SQLite's in-memory database has no stable filesystem identity, so
        # it cannot share a per-database process lock. In particular, resolving
        # this DSN as a path creates a persistent ``:memory:.collect.lock`` in
        # the caller's working tree and breaks clean-worktree verification.
        self.path = (
            None if db_path == ':memory:' else
            Path(db_path).expanduser().resolve().with_suffix(
                Path(db_path).suffix + '.collect.lock'))
        self._fd = None

    def acquire(self):
        if self.path is None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b'0')
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            self.close()
            if getattr(exc, 'errno', None) in (11, 13, 35, 36):
                return False
            raise

    def close(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.close()
