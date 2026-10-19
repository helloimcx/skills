"""CLI resolution: a configured command becomes an executable argv.

Leaf module — standard library only, no codeloop imports. The agent-runner
registry is pulled in by the control plane's command-validation surface, and
that surface must stay importable without dragging in ``trigger`` / ``cp``
behind it (they import each other's neighbours, so a registry that reached
for them would close the loop). Both the event trigger's spawn path and the
runner presets resolve their binaries through here.
"""
from __future__ import annotations

import shutil


def resolve_command(command, which_fn=None):
    """Resolve argv[0] to an executable path, bridging Windows batch shims.

    ``.cmd``/``.bat`` wrappers cannot be exec'd by CreateProcess directly, so
    they are bridged through cmd.exe with a pure argv list — the prompt never
    passes through argv. Windows caveat: ``cmd /c batch`` exits silently under
    DETACHED_PROCESS, so machine configs should bind the native executable.
    """
    resolve = which_fn or shutil.which
    resolved = resolve(command[0]) or command[0]
    if resolved.lower().endswith(('.cmd', '.bat')):
        return ['cmd.exe', '/c', resolved] + list(command[1:])
    return [resolved] + list(command[1:])
