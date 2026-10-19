#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_shim.py — Thin skill delegation helper for code-loop CLI entrypoints.

Implements the fail-closed service locator contract described in
services/code-loop/README.md:
1. Environment variable CODE_LOOP_SERVICE (must be valid directory containing
   scripts/control_plane.py, else CODE_LOOP_SERVICE_INVALID);
2. Walk-up from this file/caller through parent directories looking for
   <ancestor>/services/code-loop (covers repo root, worktrees, junction deployments);
3. Pinned service home at ~/.code-loop/service.home (single-line absolute path;
   if present but invalid, raises SERVICE_PIN_INVALID);
4. Fail-closed: exits with code 1 and SERVICE_NOT_FOUND.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys
from typing import Optional

MARKER_FILE = Path("scripts") / "control_plane.py"
DEFAULT_PIN = Path.home() / ".code-loop" / "service.home"


def find_service_root(from_file: Optional[Path] = None) -> Path:
    """Locate the code-loop service root according to priority rules."""
    # 1. Environment variable CODE_LOOP_SERVICE
    env_service = os.environ.get("CODE_LOOP_SERVICE")
    if env_service:
        candidate = Path(env_service).expanduser().resolve()
        if candidate.is_dir() and (candidate / MARKER_FILE).is_file():
            return candidate
        sys.stderr.write(f"CODE_LOOP_SERVICE_INVALID: {env_service}\n")
        sys.exit(1)

    # 2. Walk-up from the calling script or this file
    origin = Path(from_file).resolve() if from_file else Path(__file__).resolve()
    for parent in origin.parents:
        candidate = parent / "services" / "code-loop"
        if candidate.is_dir() and (candidate / MARKER_FILE).is_file():
            return candidate.resolve()

    # 3. Pinned service home file
    if DEFAULT_PIN.is_file():
        try:
            content = DEFAULT_PIN.read_text(encoding="utf-8").strip()
            if content:
                candidate = Path(content).expanduser().resolve()
                if candidate.is_dir() and (candidate / MARKER_FILE).is_file():
                    return candidate
            sys.stderr.write(f"SERVICE_PIN_INVALID: {content}\n")
            sys.exit(1)
        except SystemExit:
            raise
        except Exception as exc:
            sys.stderr.write(f"SERVICE_PIN_INVALID: {exc}\n")
            sys.exit(1)

    # 4. Fail-closed
    sys.stderr.write(
        "SERVICE_NOT_FOUND: could not locate code-loop service root "
        "(checked env CODE_LOOP_SERVICE, walk-up to services/code-loop, and ~/.code-loop/service.home)\n"
    )
    sys.exit(1)


def delegate(script_name: str, argv: Optional[list[str]] = None, from_file: Optional[Path] = None) -> None:
    """Locate service and execute target script via runpy."""
    caller = Path(from_file).resolve() if from_file else Path(__file__).resolve()
    svc_root = find_service_root(caller)
    target_script = svc_root / "scripts" / script_name

    if not target_script.is_file():
        sys.stderr.write(f"TARGET_SCRIPT_NOT_FOUND: {target_script}\n")
        sys.exit(1)

    if str(svc_root) not in sys.path:
        sys.path.insert(0, str(svc_root))

    if argv is not None:
        sys.argv = argv

    # Execute target script with its own name and globals
    runpy.run_path(str(target_script), run_name="__main__")


if __name__ == "__main__":
    root = find_service_root()
    print(f"code-loop service located at: {root}")
