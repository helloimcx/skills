"""Bounded subprocess runner for production dashboard collection rounds."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


OUTPUT_LIMIT = 1024 * 1024
DIAGNOSTIC_TAIL = 48 * 1024
DEFAULT_TIMEOUT = 300
SERVICE_ROOT = Path(__file__).resolve().parents[2]
COLLECT_SCRIPT = SERVICE_ROOT / 'scripts' / 'dashboard.py'


def _drain(stream, name, state, lock, overflow):
    try:
        while True:
            chunk = stream.read(16384)
            if not chunk:
                return
            with lock:
                state['total'] += len(chunk)
                tail = state[name]
                tail.extend(chunk)
                if len(tail) > DIAGNOSTIC_TAIL:
                    del tail[:-DIAGNOSTIC_TAIL]
                if state['total'] > OUTPUT_LIMIT:
                    overflow.set()
    finally:
        stream.close()


def _stop_child(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def _diagnostics(state):
    return 'stdout tail: %s\nstderr tail: %s' % (
        bytes(state['stdout']).decode('utf-8', 'replace'),
        bytes(state['stderr']).decode('utf-8', 'replace'))


def run_collect(db_path, root, only=None, config_path=None,
                timeout=DEFAULT_TIMEOUT, env=None):
    """Run the formal CLI and return only its bounded atomic result document."""
    try:
        if isinstance(timeout, bool) or (isinstance(timeout, float)
                                         and not timeout.is_integer()):
            raise ValueError
        timeout = int(timeout)
    except (TypeError, ValueError):
        raise ValueError('collect timeout must be an integer') from None
    if not 1 <= timeout <= 3600:
        raise ValueError('collect timeout must be between 1 and 3600 seconds')
    db_path = Path(db_path).expanduser().resolve()
    root = Path(root).expanduser().resolve()
    config_path = Path(config_path or (Path.home() / '.code-loop' / 'dashboard.json')) \
        .expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, result_name = tempfile.mkstemp(prefix='.dashboard-result-', suffix='.json',
                                       dir=str(db_path.parent))
    os.close(fd)
    os.unlink(result_name)
    argv = [sys.executable, str(COLLECT_SCRIPT), 'collect', '--db', str(db_path),
            '--root', str(root), '--config', str(config_path), '--timeout',
            str(timeout), '--result-file', result_name]
    if only:
        argv.extend(['--project', str(only)])
    process = None
    try:
        process = subprocess.Popen(argv, cwd=str(SERVICE_ROOT), env=env,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   close_fds=True, bufsize=0)
        state = {'total': 0, 'stdout': bytearray(), 'stderr': bytearray()}
        lock = threading.Lock()
        overflow = threading.Event()
        drainers = [
            threading.Thread(target=_drain,
                             args=(process.stdout, 'stdout', state, lock, overflow),
                             daemon=True),
            threading.Thread(target=_drain,
                             args=(process.stderr, 'stderr', state, lock, overflow),
                             daemon=True),
        ]
        for thread in drainers:
            thread.start()
        deadline = time.monotonic() + timeout
        failure = None
        while process.poll() is None:
            if overflow.is_set():
                failure = 'collect child exceeded the combined 1 MiB output limit'
                _stop_child(process)
                break
            if time.monotonic() >= deadline:
                failure = 'collect child exceeded %d-second deadline' % timeout
                _stop_child(process)
                break
            time.sleep(0.025)
        code = process.wait()
        for thread in drainers:
            thread.join(timeout=2)
        if failure:
            raise RuntimeError('%s\n%s' % (failure, _diagnostics(state)))
        if overflow.is_set():
            raise RuntimeError('collect child exceeded the combined 1 MiB output limit\n%s'
                               % _diagnostics(state))
        result_path = Path(result_name)
        try:
            raw = result_path.read_bytes()
        except OSError as exc:
            raise RuntimeError('collect child did not publish a result (exit %s): %s\n%s'
                               % (code, exc, _diagnostics(state))) from None
        if len(raw) > OUTPUT_LIMIT:
            raise RuntimeError('collect result exceeds the 1 MiB limit')
        try:
            result = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError('collect child published malformed result\n%s'
                               % _diagnostics(state)) from None
        if not isinstance(result, dict) or not isinstance(result.get('collect'), dict) \
                or not isinstance(result.get('notify'), dict):
            raise RuntimeError('collect child result has an invalid shape\n%s'
                               % _diagnostics(state))
        summary = result['collect']
        if summary.get('skipped'):
            if code != 0:
                raise RuntimeError('skipped collect child exited %d\n%s'
                                   % (code, _diagnostics(state)))
            return result
        if not isinstance(summary.get('projects'), int) \
                or not isinstance(summary.get('failed'), list):
            raise RuntimeError('collect child summary has an invalid shape\n%s'
                               % _diagnostics(state))
        if code not in (0, 1) or (code == 1 and not summary['failed']) \
                or (code == 0 and summary['failed']):
            raise RuntimeError('collect child exited %d\n%s' % (code, _diagnostics(state)))
        return result
    finally:
        if process is not None and process.poll() is None:
            _stop_child(process)
        try:
            os.unlink(result_name)
        except FileNotFoundError:
            pass


def make_process_refresh(db_path, root=None, config_path=None,
                         timeout=DEFAULT_TIMEOUT, env=None):
    """Production refresh callback. Collection, notify and operator ingest
    execute together in the child CLI and share its per-database kernel lock."""
    from . import collect
    collect_root = root or collect.DEFAULT_ROOT

    def refresh_once(only=None):
        return run_collect(db_path, collect_root, only=only,
                           config_path=config_path, timeout=timeout, env=env)

    return refresh_once
