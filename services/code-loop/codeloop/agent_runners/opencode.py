"""opencode plugin: an unattended run preset plus a detected model list.

``opencode run --auto`` is its unattended-permission switch, the counterpart
of the registered claude command's ``--dangerously-skip-permissions``; ``-m
<model>`` is added when a model was chosen. A requested model is re-checked
against THIS machine's ``opencode models`` first — the dialog could only ever
reflect the serve host's list.

The executable is bound NATIVELY: a resolved npm shim cannot be spawned
headlessly (``cmd /c batch`` exits silently under DETACHED_PROCESS), so a
shim without a findable target refuses here — before any claim, worktree or
session is spent.
"""
from __future__ import annotations

import shutil

from . import base, opencode_storage


def native_exe_for(path, read_fn=None):
    """The runnable executable behind an npm ``.cmd``/``.bat`` shim, or None
    when the shim's target cannot be found.

    Spawns must never bind the shim: ``resolve_command`` would bridge it
    through ``cmd.exe``, and ``cmd /c batch`` exits silently (rc 1, no
    output at all) under the DETACHED_PROCESS flag every spawn sets — the
    round then looks alive while nothing ever ran. A path that is not a shim
    is already runnable and comes back as-is. This install is a native
    binary, so the policy filters the shared shim skeleton for its first
    existing ``.exe`` target."""
    text_path = str(path or '')
    if not text_path.lower().endswith(base.SHIM_SUFFIXES):
        return text_path
    for target in base.shim_targets(text_path, read_fn=read_fn):
        if target.lower().endswith('.exe'):
            return target
    return None


class OpenCodeRunner(base.Runner):
    id = 'opencode'
    label = 'opencode'
    sub = '开源 agent'
    binary = 'opencode'
    models_command = [binary, 'models']

    def argv(self, model=None, which_fn=None, run_fn=None):
        resolved = which_fn or shutil.which
        found = resolved(self.binary)
        if not found:
            raise ValueError('AGENT_MISSING: %s 未安装在本机 PATH 上' % self.binary)
        if model is not None and not base.valid_model_id(model):
            raise ValueError('INVALID_COMMAND: model must match provider/model')
        exe = native_exe_for(found)
        if exe is None:
            raise ValueError('OPENCODE_SHIM_ONLY: %s 只有 %s 批处理垫片且找不到原生可执行文件，'
                             '无法无控制台启动（请重装 opencode 或把原生 exe 加入 PATH）'
                             % (self.binary, found))
        preset = [exe, 'run', '--auto', '--format', 'json']
        if model is None:
            return preset
        available = self.models(run_fn=run_fn, which_fn=resolved)
        if model not in available:
            raise ValueError('MODEL_UNAVAILABLE: %s 不在本机 opencode models 列表（可用：%s）'
                             % (model, ', '.join(available[:8]) or '无'))
        return preset + ['-m', model]

    # -- output face: opencode's JSON session storage ------------------------
    def resolve_session(self, repo_cwd, exec_id, created_at):
        return opencode_storage.resolve_session(repo_cwd, exec_id, created_at)

    def tail(self, handle, offset=0, limit=None):
        return opencode_storage.tail(handle, offset=offset, limit=limit)


RUNNER = OpenCodeRunner()
