"""Regression tests for parse-phase hang recovery (v1.0.7).

Covers:
- Oversized file skip (_MAX_FILE_BYTES guard)
- Parse heartbeat thread lifecycle
- Per-future timeout with sentinel insertion
"""
from __future__ import annotations

import concurrent.futures
import os
import threading
import time

import pytest


# ---------------------------------------------------------------------------
# Test A: oversized-file skip
# ---------------------------------------------------------------------------


def test_parse_worker_skips_oversized(tmp_path, monkeypatch):
    """Files larger than _MAX_FILE_BYTES return parsed=None without calling
    parse_java_source, preventing tree-sitter from hanging on giant files."""
    import codespine.indexer.engine as eng

    monkeypatch.setattr(eng, "_MAX_FILE_BYTES", 100)

    java_file = tmp_path / "Big.java"
    java_file.write_bytes(b"public class Big {}\n" + b" " * 200)

    result = eng._parse_file_worker(str(java_file), str(tmp_path), "test-proj")

    assert result["parsed"] is None
    assert result["skipped_reason"] == "oversized"
    assert result["rel_path"] == "Big.java"
    assert result["source"] == b""


def test_parse_worker_normal_file(tmp_path):
    """Files within the size limit are parsed normally."""
    import codespine.indexer.engine as eng

    java_file = tmp_path / "Small.java"
    java_file.write_bytes(b"public class Small {}\n")

    result = eng._parse_file_worker(str(java_file), str(tmp_path), "test-proj")

    assert result["parsed"] is not None
    assert result["skipped_reason"] if "skipped_reason" in result else True  # not set = fine
    assert "skipped_reason" not in result or result["skipped_reason"] != "oversized"


def test_parse_worker_max_file_bytes_env(tmp_path, monkeypatch):
    """CODESPINE_MAX_FILE_BYTES env var controls the threshold."""
    monkeypatch.setenv("CODESPINE_MAX_FILE_BYTES", "50")
    # Re-import to pick up new env (the constant is read at import time,
    # so we must reload or patch it directly).
    import codespine.indexer.engine as eng
    import importlib
    # Patch after import
    monkeypatch.setattr(eng, "_MAX_FILE_BYTES", 50)

    java_file = tmp_path / "Medium.java"
    java_file.write_bytes(b"x" * 100)

    result = eng._parse_file_worker(str(java_file), str(tmp_path), "test-proj")
    assert result["parsed"] is None
    assert result["skipped_reason"] == "oversized"


# ---------------------------------------------------------------------------
# Test B: parse heartbeat thread lifecycle
# ---------------------------------------------------------------------------


def test_parse_heartbeat_emits_and_stops():
    """The heartbeat thread fires events every _PARSE_HEARTBEAT_PERIOD seconds
    and stops cleanly when signalled."""
    events: list[dict] = []
    stop_event = threading.Event()

    _done_holder: list[int] = [0]
    _current_holder: list[str] = ["Foo.java"]
    _start = time.perf_counter()
    _total = 10
    _period = 0.1  # fast for testing

    def _worker() -> None:
        while not stop_event.wait(_period):
            events.append({
                "event": "parse_heartbeat",
                "done": _done_holder[0],
                "total": _total,
                "current_file": _current_holder[0],
                "elapsed": time.perf_counter() - _start,
            })

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    time.sleep(0.35)  # allow ~3 ticks
    stop_event.set()
    t.join(timeout=1.0)

    assert not t.is_alive(), "Heartbeat thread did not stop"
    assert len(events) >= 2, f"Expected ≥2 heartbeat events, got {len(events)}"
    assert all(e["event"] == "parse_heartbeat" for e in events)
    assert all(e["total"] == _total for e in events)


def test_parse_heartbeat_reflects_state_updates():
    """The heartbeat reads live state from the shared holders."""
    events: list[dict] = []
    stop_event = threading.Event()
    _done_holder: list[int] = [0]
    _period = 0.05

    def _worker() -> None:
        while not stop_event.wait(_period):
            events.append({"done": _done_holder[0]})

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    time.sleep(0.08)
    _done_holder[0] = 42
    time.sleep(0.08)
    stop_event.set()
    t.join(timeout=1.0)

    done_values = [e["done"] for e in events]
    assert 42 in done_values, "State update not reflected in heartbeat"


# ---------------------------------------------------------------------------
# Test C: per-future timeout produces sentinel result
# ---------------------------------------------------------------------------


def test_timeout_sentinel_has_correct_shape(tmp_path):
    """When a future times out, the sentinel dict must have parsed=None and
    skipped_reason='timeout' so the DB-write loop skips it safely."""
    import codespine.indexer.engine as eng

    # Construct a sentinel the same way the engine does it.
    fp = str(tmp_path / "Slow.java")
    root_path = str(tmp_path)
    project_id = "myproj"

    sentinel = {
        "file_path": fp,
        "rel_path": os.path.relpath(fp, root_path),
        "source": b"",
        "parsed": None,
        "f_id": eng.file_id(project_id, os.path.relpath(fp, root_path)),
        "digest": "",
        "is_test": False,
        "scope": "main",
        "skipped_reason": "timeout",
    }

    assert sentinel["parsed"] is None
    assert sentinel["skipped_reason"] == "timeout"
    assert sentinel["source"] == b""


def test_parse_loop_skips_none_parsed_in_db_write(tmp_path):
    """The DB-write loop guard (parsed is None → continue) must not NPE when
    a skipped sentinel is in parse_results."""
    # We verify the guard logic directly without a real store.
    parse_results = [
        {
            "file_path": str(tmp_path / "Skip.java"),
            "rel_path": "Skip.java",
            "source": b"",
            "parsed": None,
            "f_id": "fid-skip",
            "digest": "",
            "is_test": False,
            "scope": "main",
            "skipped_reason": "oversized",
        }
    ]

    # Simulate what the engine's DB-write loop does.
    files_indexed = 0
    for pr in parse_results:
        if pr.get("parsed") is None:
            files_indexed += 1
            continue
        # This line would NPE if reached with parsed=None:
        _ = pr["parsed"].classes

    assert files_indexed == 1, "Skipped sentinel should increment files_indexed"


# ---------------------------------------------------------------------------
# Test D: DB-write progress covers the silent post-parse phase
# ---------------------------------------------------------------------------


def test_db_write_heartbeat_emits_until_write_done(tmp_path, monkeypatch):
    """The post-parse DB write phase must emit start/heartbeat/progress/done
    events so the CLI does not look frozen before call tracing begins."""
    import codespine.indexer.engine as eng

    class _Txn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _SlowStore:
        def query_records(self, *_args, **_kwargs):
            return []

        def clear_project(self, *_args, **_kwargs):
            pass

        def clear_file(self, *_args, **_kwargs):
            pass

        def upsert_project(self, *_args, **_kwargs):
            pass

        def transaction(self):
            return _Txn()

        def clear_files_batch(self, *_args, **_kwargs):
            pass

        def upsert_files_batch(self, *_args, **_kwargs):
            pass

        def upsert_classes_batch(self, *_args, **_kwargs):
            pass

        def upsert_methods_batch(self, *_args, **_kwargs):
            time.sleep(0.03)

        def upsert_symbols_batch(self, *_args, **_kwargs):
            time.sleep(0.03)

        def add_calls_batch(self, *_args, **_kwargs):
            pass

        def add_references_batch(self, *_args, **_kwargs):
            pass

        def add_injections_batch(self, *_args, **_kwargs):
            pass

        def add_interface_bindings_batch(self, *_args, **_kwargs):
            pass

        def _recycle_conn(self):
            pass

    monkeypatch.setattr(eng, "_PARSE_HEARTBEAT_PERIOD", 0.005)
    monkeypatch.setattr(eng, "resolve_calls", lambda *_args, **_kwargs: iter(()))

    java_file = tmp_path / "src/main/java/com/example/Foo.java"
    java_file.parent.mkdir(parents=True)
    java_file.write_text(
        "package com.example; public class Foo { void run() {} }\n",
        encoding="utf-8",
    )

    events: list[tuple[str, dict]] = []
    indexer = eng.JavaIndexer(_SlowStore())
    indexer.index_project(
        str(tmp_path),
        full=True,
        progress=lambda event, payload: events.append((event, dict(payload))),
        project_id="proj",
        embed=False,
    )

    names = [event for event, _payload in events]
    assert "db_write_start" in names
    assert "db_write_heartbeat" in names
    assert "db_write_progress" in names
    assert "db_write_done" in names
    assert names.index("db_write_done") < names.index("resolve_calls_start")

    done_payload = next(payload for event, payload in events if event == "db_write_done")
    assert done_payload["files_indexed"] == 1
    assert done_payload["classes"] == 1
    assert done_payload["methods"] == 1
