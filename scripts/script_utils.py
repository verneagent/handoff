#!/usr/bin/env python3
"""Shared helpers for handoff CLI scripts."""

import os
import subprocess
import sys
from typing import Sequence


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def script_path(name: str) -> str:
    """Return the absolute path to a helper script in this directory."""

    return os.path.join(SCRIPT_DIR, name)


def run_tool(
    description: str, script: str, *args: str, capture: bool = False
) -> subprocess.CompletedProcess:
    """Run another helper script via python and bubble up its exit code."""

    cmd: Sequence[str] = [sys.executable, script_path(script), *args]
    result = subprocess.run(cmd, capture_output=capture, text=True)
    if capture:
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result
