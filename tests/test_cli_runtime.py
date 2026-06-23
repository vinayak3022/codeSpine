from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from codespine import cli


def test_run_subprocess_with_retries_retries_until_success(tmp_path: Path):
    log_path = tmp_path / "run.log"
    calls = {"count": 0}

    def fake_run(cmd, cwd=None, stdout=None, stderr=None, check=None):
        calls["count"] += 1
        return SimpleNamespace(returncode=0 if calls["count"] == 3 else 1)

    original = cli.subprocess.run
    cli.subprocess.run = fake_run
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = cli._run_subprocess_with_retries(["x"], log_file=log, attempts=3, initial_backoff_s=0)
    finally:
        cli.subprocess.run = original

    assert proc.returncode == 0
    assert calls["count"] == 3


def test_count_codespine_processes_ignores_access_failures(monkeypatch):
    class _Proc:
        def __init__(self, cmd):
            self._cmd = cmd

        def cmdline(self):
            if self._cmd is None:
                raise RuntimeError("denied")
            return self._cmd

    monkeypatch.setattr(cli.psutil, "process_iter", lambda *args, **kwargs: [_Proc(["python", "-m", "codespine.cli", "watch"]), _Proc(None)])
    assert cli._count_codespine_processes("watch") == 1
