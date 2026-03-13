from __future__ import annotations

import os
import subprocess


def _run_git(args: list[str], cwd: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def git_repo_root(path: str) -> str | None:
    abs_path = os.path.abspath(path)
    if os.path.isfile(abs_path):
        abs_path = os.path.dirname(abs_path)
    return _run_git(["rev-parse", "--show-toplevel"], abs_path)


def current_head(path: str) -> str | None:
    abs_path = os.path.abspath(path)
    if os.path.isfile(abs_path):
        abs_path = os.path.dirname(abs_path)
    return _run_git(["rev-parse", "HEAD"], abs_path)
