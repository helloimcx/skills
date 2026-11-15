"""Detached PR repair runtime and privacy-filtered output sink.

The sink reports the direct runtime result and then drains inherited stdout.
It does not provide a process-tree containment or descendant cleanup boundary.
"""
import json
import os
import signal
import subprocess
import sys
import threading


def _run(config, report=None):
    from .worker_task_executor import RUNTIME_STDIN_MARKER, WorkerTaskExecutor

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(config['log_path'], flags, 0o600)
    if hasattr(os, 'fchmod'):
        os.fchmod(fd, 0o600)
    log = os.fdopen(fd, 'wb')
    read_fd, write_fd = os.pipe()
    kwargs = {'cwd': config['cwd'], 'stdin': subprocess.PIPE,
              'stdout': write_fd, 'stderr': subprocess.STDOUT,
              'env': config.get('env'), 'bufsize': 0}
    if os.name == 'nt':
        kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                   | getattr(subprocess, 'DETACHED_PROCESS', 0))
    else:
        kwargs['start_new_session'] = True
    try:
        proc = subprocess.Popen(config['argv'] + [RUNTIME_STDIN_MARKER], **kwargs)
    except Exception:
        os.close(read_fd)
        os.close(write_fd)
        log.close()
        raise

    stopped = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
        try:
            if os.name == 'nt':
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    failed = []
    flushed = threading.Event()
    key = os.urandom(32)
    marker = b'CODE_LOOP_FLUSH_' + os.urandom(32).hex().encode('ascii')
    def project():
        try:
            with os.fdopen(os.dup(read_fd), 'rb', buffering=0) as stream:
                for raw in iter(stream.readline, b''):
                    if raw.rstrip(b'\r\n') == marker:
                        log.flush()
                        flushed.set()
                        break
                    safe = WorkerTaskExecutor._repair_log_event(
                        raw.decode('utf-8', errors='replace'),
                        config['agent_name'], config['execution_id'], key)
                    if safe:
                        log.write(safe.encode('utf-8'))
                        log.flush()
        except (OSError, ValueError, RecursionError):
            failed.append(True)
            flushed.set()
        finally:
            log.close()

    reader = threading.Thread(target=project, daemon=True)
    reader.start()
    def write_prompt():
        try:
            data = memoryview(config['prompt'].encode('utf-8'))
            while data:
                written = proc.stdin.write(data)
                if not written:
                    break
                data = data[written:]
        except (OSError, ValueError):
            pass  # The direct runtime may exit without reading all of stdin.
        finally:
            proc.stdin.close()
    threading.Thread(target=write_prompt, daemon=True).start()
    try:
        returncode = proc.wait(timeout=config['timeout'])
    except subprocess.TimeoutExpired:
        stop(None, None)
        returncode = proc.wait()
    try:
        # The marker follows all direct-runtime writes in the pipe. A newline
        # also terminates an incomplete final line before the marker.
        os.write(write_fd, b'\n' + marker + b'\n')
    except OSError:
        failed.append(True)
        flushed.set()
    finally:
        os.close(write_fd)
    if not flushed.wait(timeout=5) or failed:
        status = 2
    elif stopped:
        status = 124
    else:
        status = 0 if returncode == 0 else 1
    reader.join(timeout=5)
    try:
        # A background child may still hold stdout. Transfer draining to a
        # short-lived helper so this journaled sink can exit with the direct
        # runtime; background writers retain a reader until they close it.
        drainer_kwargs = {'stdin': read_fd, 'stdout': subprocess.DEVNULL,
                          'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            drainer_kwargs['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0)
                                               | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            drainer_kwargs['start_new_session'] = True
        subprocess.Popen([sys.executable, '-c',
                          'import sys\nwhile sys.stdin.buffer.read(65536): pass'],
                         **drainer_kwargs)
    except OSError:
        status = 2
    finally:
        os.close(read_fd)
    if report is not None:
        try:
            report(status)
        except OSError:
            pass  # WorkerAgent may have exited; continue draining the pipe.
    return status


def main():
    try:
        config = json.load(sys.stdin)
        if not isinstance(config, dict):
            return 2
        def report(status):
            os.write(sys.stdout.fileno(), ('%s\n' % status).encode('ascii'))
        return _run(config, report=report)
    except Exception:
        # Raw runtime output and exceptions never reach WorkerAgent diagnostics.
        return 2


if __name__ == '__main__':
    sys.exit(main())
