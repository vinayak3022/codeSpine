"""Git hook installer for CodeSpine post-commit re-indexing.

The post-commit hook triggers ``codespine analyse <path> --incremental``
immediately after each commit so the graph is always up to date without
waiting for the next poll cycle.

Usage
-----
    from codespine.watch.git_hook import install_post_commit_hook, uninstall_post_commit_hook

    install_post_commit_hook("/path/to/java-project")
    uninstall_post_commit_hook("/path/to/java-project")

The hook is intentionally opt-in — it is **not** installed automatically.
Users trigger installation via ``codespine watch --install-hook`` or the
``start_watch()`` MCP tool with ``install_hook=True``.
"""

from __future__ import annotations

import os
import stat
import sys

_MARKER = "# codespine-hook"

_HOOK_TEMPLATE = """\
#!/bin/sh
{marker}
# Auto-installed by CodeSpine.  Remove lines between the markers to uninstall.
{python} -m codespine.cli analyse "{project_path}" --incremental --allow-running \\
    >> "${{HOME}}/.codespine_hook.log" 2>&1 &
# End {marker}
"""


def _hooks_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".git", "hooks")


def _hook_path(repo_root: str) -> str:
    return os.path.join(_hooks_dir(repo_root), "post-commit")


def install_post_commit_hook(project_path: str) -> str:
    """Write (or append) a post-commit hook for *project_path*.

    Returns the hook file path on success.
    Raises ``RuntimeError`` if the path is not inside a git repository.
    """
    abs_path = os.path.abspath(project_path)
    # Locate the git root (may differ from project_path for mono-repos).
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=abs_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{abs_path!r} is not inside a git repository")
        git_dir = result.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(abs_path, git_dir)
    except FileNotFoundError:
        raise RuntimeError("git is not installed or not on PATH")

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_file = os.path.join(hooks_dir, "post-commit")

    python = sys.executable
    new_block = _HOOK_TEMPLATE.format(
        marker=_MARKER,
        python=python,
        project_path=abs_path,
    )

    if os.path.exists(hook_file):
        with open(hook_file, "r", encoding="utf-8") as fh:
            existing = fh.read()
        if _MARKER in existing:
            # Already installed — replace the existing block in-place.
            lines = existing.splitlines(keepends=True)
            out_lines: list[str] = []
            inside = False
            replaced = False
            for line in lines:
                if _MARKER in line and not inside and not replaced:
                    inside = True
                    out_lines.append(new_block)
                    replaced = True
                    continue
                if inside:
                    if f"End {_MARKER}" in line:
                        inside = False
                    continue
                out_lines.append(line)
            content = "".join(out_lines)
        else:
            # Append to existing hook.
            content = existing.rstrip("\n") + "\n\n" + new_block
    else:
        content = "#!/bin/sh\n" + new_block

    tmp = hook_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, hook_file)
    # Ensure the hook is executable.
    current_mode = os.stat(hook_file).st_mode
    os.chmod(hook_file, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook_file


def uninstall_post_commit_hook(project_path: str) -> bool:
    """Remove the CodeSpine block from the post-commit hook.

    Returns True if the block was found and removed, False otherwise.
    If the hook becomes empty after removal it is deleted entirely.
    """
    abs_path = os.path.abspath(project_path)
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=abs_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        git_dir = result.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(abs_path, git_dir)
    except FileNotFoundError:
        return False

    hook_file = os.path.join(git_dir, "hooks", "post-commit")
    if not os.path.exists(hook_file):
        return False

    with open(hook_file, "r", encoding="utf-8") as fh:
        existing = fh.read()
    if _MARKER not in existing:
        return False

    lines = existing.splitlines(keepends=True)
    out_lines: list[str] = []
    inside = False
    for line in lines:
        if _MARKER in line and not inside:
            inside = True
            continue
        if inside:
            if f"End {_MARKER}" in line:
                inside = False
            continue
        out_lines.append(line)

    content = "".join(out_lines).strip()
    if not content or content == "#!/bin/sh":
        os.remove(hook_file)
    else:
        tmp = hook_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content + "\n")
        os.replace(tmp, hook_file)
    return True


def hook_is_installed(project_path: str) -> bool:
    """Return True if the CodeSpine post-commit hook is present."""
    abs_path = os.path.abspath(project_path)
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=abs_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        git_dir = result.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(abs_path, git_dir)
    except FileNotFoundError:
        return False

    hook_file = os.path.join(git_dir, "hooks", "post-commit")
    if not os.path.exists(hook_file):
        return False
    try:
        with open(hook_file, "r", encoding="utf-8") as fh:
            return _MARKER in fh.read()
    except OSError:
        return False
