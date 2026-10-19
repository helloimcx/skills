"""pi plugin: a node-entry spawn preset plus a detected model table.

``pi -p`` is its unattended mode, and it needs no permission switch: pi has
no per-tool approval gate — its security model is project trust (what gets
loaded), which non-interactive runs skip by design. The prompt arrives on
stdin (the spawn feeds the round's own prompt file); a non-TTY stdin/stdout
pair selects the print mode on its own. ``--model <provider/model>`` is
added when a model was chosen, re-checked against THIS machine's
``pi --list-models`` table first — the dialog could only ever reflect the
serve host's list.

The executable is bound NATIVELY, one step further than opencode: pi ships
no native exe — its npm shim calls ``node  <…>/cli.js`` — so a resolved
shim must yield both the script and a node interpreter, or refuse here,
before any claim, worktree or session is spent.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..cli import resolve_command
from . import base, pi_transcript


def node_entry_for(path, read_fn=None, which_fn=None):
    """The argv head for a resolved pi binary: ``[node, cli.js]`` behind an
    npm shim, the path itself (as a one-element list) when it is not a shim,
    None when the shim's script or a node interpreter cannot be found.

    Spawns must never bind the shim (``cmd /c batch`` dies silently under
    the DETACHED_PROCESS flag every spawn sets), so the shim is unwound all
    the way to a real interpreter + script pair. node is looked up beside
    the shim first — the same ``IF EXIST "%dp0%\\node.exe"`` order the shim
    itself uses — then on PATH."""
    text_path = str(path or '')
    if not text_path.lower().endswith(base.SHIM_SUFFIXES):
        return [text_path]
    script = next((t for t in base.shim_targets(text_path, read_fn=read_fn)
                   if t.lower().endswith('.js')), None)
    if script is None:
        return None
    resolved = which_fn or shutil.which
    beside = Path(text_path).parent / 'node.exe'
    node = str(beside) if beside.is_file() else resolved('node')
    return [node, script] if node else None


class PiRunner(base.Runner):
    id = 'pi'
    label = 'pi'
    sub = 'pi coding agent'
    binary = 'pi'
    models_command = [binary, '--list-models']

    def models(self, run_fn=None, which_fn=None,
               timeout=base.DEFAULT_MODELS_TIMEOUT):
        """``--list-models`` prints an aligned table — header row included,
        whose own tokens would pass a naive one-token-per-line parse — so
        the provider and model columns are taken instead: joined as
        ``provider/model`` (a model column that already carries a slash is
        used verbatim), the header skipped. Any failure — missing binary,
        timeout, non-zero exit — degrades to an empty list: a broken listing
        costs the picker, never the endpoint that serves it."""
        resolved = which_fn or ((lambda cmd: cmd) if run_fn is not None else shutil.which)
        if not resolved(self.binary):
            return []
        run = run_fn or subprocess.run
        try:
            proc = run(resolve_command(self.models_command, which_fn=resolved),
                       capture_output=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0:
            return []
        stdout = proc.stdout or ''
        if isinstance(stdout, bytes):
            stdout = stdout.decode('utf-8', errors='replace')
        models = []
        for line in stdout.splitlines():
            columns = line.split()
            # A real table row carries six columns; demanding a third keeps
            # summary lines ("2 models …") out of the picker.
            if len(columns) < 3 or columns[0] == 'provider':
                continue
            token = columns[1] if '/' in columns[1] \
                else columns[0] + '/' + columns[1]
            if base.valid_model_id(token) and token not in models:
                models.append(token)
        return models

    def argv(self, model=None, which_fn=None, run_fn=None):
        resolved = which_fn or ((lambda cmd: cmd) if run_fn is not None else shutil.which)
        found = resolved(self.binary)
        if not found:
            raise ValueError('AGENT_MISSING: %s 未安装在本机 PATH 上' % self.binary)
        if model is not None and not base.valid_model_id(model):
            raise ValueError('INVALID_COMMAND: model must match provider/model')
        entry = node_entry_for(found, which_fn=resolved)
        if entry is None:
            raise ValueError('PI_SHIM_ONLY: %s 只有 npm 批处理垫片且解析不出 node 入口'
                             '（node + cli.js），无法无控制台启动'
                             '（请重装 pi 或把原生 node 加入 PATH）' % found)
        preset = entry + ['-p']
        if model is None:
            return preset
        available = self.models(run_fn=run_fn, which_fn=resolved)
        if model not in available:
            raise ValueError('MODEL_UNAVAILABLE: %s 不在本机 pi --list-models 列表'
                             '（可用：%s）' % (model, ', '.join(available[:8]) or '无'))
        return preset + ['--model', model]

    # -- output face: pi's JSONL session storage ------------------------------
    def resolve_session(self, repo_cwd, exec_id, created_at):
        return pi_transcript.resolve_session(repo_cwd, exec_id, created_at)

    def tail(self, handle, offset=0, limit=None):
        return pi_transcript.tail(handle, offset=offset, limit=limit)


RUNNER = PiRunner()
