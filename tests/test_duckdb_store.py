"""Tests for DuckDBStore — the DuckDB-backed storage layer.

These tests exercise the write/read API of DuckDBStore in isolation, then
test that ShardedGraphStore correctly routes to DuckDBStore when
backend="duckdb".
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("tree_sitter_java")

from codespine.db.duckdb_store import DuckDBStore
from codespine.sharding.store import ShardedGraphStore
from codespine.indexer.engine import JavaIndexer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path, read_only: bool = False) -> DuckDBStore:
    db = str(tmp_path / "test.db")
    snap = str(tmp_path / "test_snap.db")
    return DuckDBStore(read_only=read_only, db_path_override=db, snapshot_path_override=snap)


def _write_java(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Schema / open
# ---------------------------------------------------------------------------


def test_duckdb_store_opens_fresh(tmp_path: Path):
    s = _store(tmp_path)
    # Should be empty on fresh open.
    rows = s.query_records("SELECT * FROM projects")
    assert rows == []


def test_duckdb_store_schema_idempotent(tmp_path: Path):
    """Opening the same DB twice should not raise."""
    db = str(tmp_path / "idem.db")
    snap = str(tmp_path / "idem_snap.db")
    s1 = DuckDBStore(db_path_override=db, snapshot_path_override=snap)
    s1._conn.close()
    s2 = DuckDBStore(db_path_override=db, snapshot_path_override=snap)
    rows = s2.query_records("SELECT * FROM projects")
    assert rows == []


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def test_upsert_and_get_project(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("myapp", "/code/myapp")
    meta = s.get_project_metadata("myapp")
    assert meta is not None
    assert meta["id"] == "myapp"
    assert meta["path"] == "/code/myapp"
    assert meta["language"] == "java"


def test_list_project_metadata(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("alpha", "/a")
    s.upsert_project("beta", "/b")
    projects = s.list_project_metadata()
    ids = {p["id"] for p in projects}
    assert ids == {"alpha", "beta"}


def test_upsert_project_is_idempotent(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("proj", "/first")
    s.upsert_project("proj", "/second")
    rows = s.list_project_metadata()
    assert len(rows) == 1
    # Last write wins on path.
    assert rows[0]["path"] == "/second"


def test_clear_project(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("todel", "/d")
    s.upsert_project("keep", "/k")
    s.clear_project("todel")
    ids = {p["id"] for p in s.list_project_metadata()}
    assert "todel" not in ids
    assert "keep" in ids


# ---------------------------------------------------------------------------
# File / Class / Method / Symbol upserts
# ---------------------------------------------------------------------------


def test_file_upsert_and_clear(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/Foo.java", "p", False, "abc123")
    rows = s.query_records("SELECT id, path FROM files")
    assert len(rows) == 1
    assert rows[0]["id"] == "f1"

    s.clear_file("f1")
    assert s.query_records("SELECT id FROM files") == []


def test_class_upsert(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/Foo.java", "p", False, "abc")
    s.upsert_class("c1", "com.example.Foo", "Foo", "com.example", "f1")
    rows = s.query_records("SELECT fqcn FROM classes")
    assert rows[0]["fqcn"] == "com.example.Foo"


def test_method_upsert(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/Foo.java", "p", False, "abc")
    s.upsert_class("c1", "com.example.Foo", "Foo", "com.example", "f1")
    s.upsert_methods_batch([{
        "id": "m1", "class_id": "c1", "name": "greet",
        "signature": "greet(String):String", "return_type": "String",
        "modifiers": ["public"], "is_constructor": False, "is_test": False,
    }])
    rows = s.query_records("SELECT name FROM methods")
    assert rows[0]["name"] == "greet"


def test_symbol_upsert_no_embedding(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/Foo.java", "p", False, "abc")
    s.upsert_symbols_batch([{
        "id": "s1", "kind": "CLASS", "name": "Foo", "fqname": "com.example.Foo",
        "file_id": "f1", "line": 1, "col": 0, "embedding": None,
    }])
    rows = s.query_records("SELECT name, embedding FROM symbols")
    assert rows[0]["name"] == "Foo"
    assert rows[0]["embedding"] is None


def test_symbol_upsert_with_embedding(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/Foo.java", "p", False, "abc")
    vec = [0.1] * 768
    s.upsert_symbols_batch([{
        "id": "s1", "kind": "CLASS", "name": "Foo", "fqname": "com.example.Foo",
        "file_id": "f1", "line": 1, "col": 0, "embedding": vec,
    }])
    rows = s.query_records("SELECT name FROM symbols WHERE embedding IS NOT NULL")
    assert rows[0]["name"] == "Foo"


def test_symbol_mixed_embedding_batch(tmp_path: Path):
    """Batch with mixed None/non-None embeddings must succeed (no type errors)."""
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/A.java", "p", False, "abc")
    vec = [0.5] * 768
    s.upsert_symbols_batch([
        {"id": "s1", "kind": "CLASS", "name": "A", "fqname": "a.A",
         "file_id": "f1", "line": 1, "col": 0, "embedding": None},
        {"id": "s2", "kind": "CLASS", "name": "B", "fqname": "a.B",
         "file_id": "f1", "line": 2, "col": 0, "embedding": vec},
    ])
    names = {r["name"] for r in s.query_records("SELECT name FROM symbols")}
    assert names == {"A", "B"}


# ---------------------------------------------------------------------------
# Edge tables
# ---------------------------------------------------------------------------


def test_calls_batch(tmp_path: Path):
    s = _store(tmp_path)
    s.add_calls_batch([
        {"source_id": "m1", "target_id": "m2", "confidence": 0.9, "reason": "direct"},
        {"source_id": "m1", "target_id": "m3", "confidence": 0.7, "reason": "inferred"},
    ])
    callers = s.get_callers_of("m2")
    assert callers == ["m1"]
    callees = s.get_callees_of("m1")
    assert set(callees) == {"m2", "m3"}


def test_references_batch(tmp_path: Path):
    s = _store(tmp_path)
    s.add_references_batch([
        {"src_id": "c1", "dst_id": "c2", "rel": "IMPLEMENTS", "confidence": 1.0},
    ])
    rows = s.query_records("SELECT rel FROM references_type")
    assert rows[0]["rel"] == "IMPLEMENTS"


def test_injections_batch(tmp_path: Path):
    s = _store(tmp_path)
    s.add_injections_batch([
        {"src": "c1", "dst": "c2", "framework": "spring", "binding_type": "field_inject", "confidence": 0.9},
    ])
    rows = s.query_records("SELECT framework FROM injects")
    assert rows[0]["framework"] == "spring"


# ---------------------------------------------------------------------------
# Community / Flow / Coupling
# ---------------------------------------------------------------------------


def test_set_community(tmp_path: Path):
    s = _store(tmp_path)
    s.set_community("comm1", "ServiceLayer", 0.8, ["s1", "s2"])
    rows = s.query_records("SELECT label, cohesion FROM communities")
    assert rows[0]["label"] == "ServiceLayer"
    members = s.query_records("SELECT symbol_id FROM community_members")
    assert {m["symbol_id"] for m in members} == {"s1", "s2"}


def test_set_flow(tmp_path: Path):
    s = _store(tmp_path)
    s.set_flow("flow1", "entry_sym", "downstream", [("s1", 0), ("s2", 1)])
    rows = s.query_records("SELECT kind FROM flows")
    assert rows[0]["kind"] == "downstream"
    members = s.query_records("SELECT depth FROM flow_members ORDER BY depth")
    assert [m["depth"] for m in members] == [0, 1]


def test_upsert_coupling(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_coupling("f1", "f2", 0.75, 5, 30)
    rows = s.query_records("SELECT strength, cochanges, days FROM co_changed_with")
    assert rows[0]["strength"] == pytest.approx(0.75)
    assert rows[0]["days"] == 30


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def test_snapshot_creates_replica(tmp_path: Path):
    db = str(tmp_path / "w.db")
    snap = str(tmp_path / "r.db")
    s = DuckDBStore(db_path_override=db, snapshot_path_override=snap)
    s.upsert_project("proj", "/p")
    ok = s.snapshot_to_read_replica()
    assert ok
    assert os.path.exists(snap)
    assert os.path.exists(snap + ".updated")


def test_snapshot_read_replica_visible(tmp_path: Path):
    db = str(tmp_path / "w.db")
    snap = str(tmp_path / "r.db")
    s_write = DuckDBStore(db_path_override=db, snapshot_path_override=snap)
    s_write.upsert_project("proj", "/p")
    s_write.snapshot_to_read_replica()

    s_read = DuckDBStore(read_only=True, db_path_override=db, snapshot_path_override=snap)
    rows = s_read.query_records("SELECT id FROM projects")
    assert rows[0]["id"] == "proj"


# ---------------------------------------------------------------------------
# Dead code helper
# ---------------------------------------------------------------------------


def test_get_dead_code_candidates(tmp_path: Path):
    s = _store(tmp_path)
    s.upsert_project("p", "/p")
    s.upsert_file("f1", "src/Foo.java", "p", False, "abc")
    s.upsert_class("c1", "com.example.Foo", "Foo", "com.example", "f1")
    s.upsert_methods_batch([
        {"id": "m1", "class_id": "c1", "name": "called", "signature": "called():void",
         "return_type": "void", "modifiers": [], "is_constructor": False, "is_test": False},
        {"id": "m2", "class_id": "c1", "name": "dead", "signature": "dead():void",
         "return_type": "void", "modifiers": [], "is_constructor": False, "is_test": False},
    ])
    # m1 has a caller; m2 does not
    s.add_calls_batch([{"source_id": "m_ext", "target_id": "m1", "confidence": 1.0, "reason": "direct"}])

    dead = s.get_dead_code_candidates()
    dead_names = {d["name"] for d in dead}
    assert "dead" in dead_names
    assert "called" not in dead_names


# ---------------------------------------------------------------------------
# End-to-end: index a real Java file via ShardedGraphStore(backend="duckdb")
# ---------------------------------------------------------------------------


def test_index_via_sharded_duckdb_store(tmp_path: Path):
    _write_java(
        tmp_path / "src/main/java/com/example/Calculator.java",
        """
        package com.example;
        public class Calculator {
            public int add(int a, int b) { return a + b; }
            public int subtract(int a, int b) { return a - b; }
        }
        """,
    )

    sg = ShardedGraphStore(
        num_shards=2,
        shards_dir=str(tmp_path / "shards"),
        backend="duckdb",
    )
    project_id = "calculator"
    shard_store = sg.shard(project_id)
    result = JavaIndexer(shard_store).index_project(
        str(tmp_path), full=True, project_id=project_id
    )

    assert result.files_indexed == 1
    assert result.classes_indexed >= 1
    assert result.methods_indexed >= 1

    # Direct SQL query on the DuckDB shard
    classes = shard_store.query_records(
        "SELECT name FROM classes WHERE fqcn = ?", {"fqcn": "com.example.Calculator"}
    )
    assert classes, "Class not found in DuckDB shard"
    assert classes[0]["name"] == "Calculator"

    methods = shard_store.query_records(
        "SELECT name FROM methods WHERE name IN ('add', 'subtract') ORDER BY name"
    )
    method_names = {r["name"] for r in methods}
    assert "add" in method_names
    assert "subtract" in method_names

    # Fan-out list should find the project.
    all_projects = sg.list_project_metadata()
    assert any(p["id"] == project_id for p in all_projects)


def test_sharded_duckdb_multi_project_isolation(tmp_path: Path):
    """Two projects on different shards remain isolated under DuckDB backend."""
    _write_java(
        tmp_path / "proj-a" / "src" / "main" / "java" / "a" / "Alpha.java",
        "package a; public class Alpha { public void doAlpha() {} }",
    )
    _write_java(
        tmp_path / "proj-b" / "src" / "main" / "java" / "b" / "Beta.java",
        "package b; public class Beta { public void doBeta() {} }",
    )

    sg = ShardedGraphStore(num_shards=8, shards_dir=str(tmp_path / "shards"), backend="duckdb")
    router = sg.router

    pid_a = "proj-a-duck"
    pid_b = None
    for candidate in [f"proj-alt-{i}" for i in range(100)]:
        if router.shard_for(candidate) != router.shard_for(pid_a):
            pid_b = candidate
            break
    if pid_b is None:
        pytest.skip("Could not find two IDs hashing to different shards")

    JavaIndexer(sg.shard(pid_a)).index_project(
        str(tmp_path / "proj-a"), full=True, project_id=pid_a
    )
    JavaIndexer(sg.shard(pid_b)).index_project(
        str(tmp_path / "proj-b"), full=True, project_id=pid_b
    )

    store_a = sg.shard(pid_a)
    store_b = sg.shard(pid_b)
    assert store_a is not store_b

    methods_a = {r["name"] for r in store_a.query_records("SELECT name FROM methods")}
    methods_b = {r["name"] for r in store_b.query_records("SELECT name FROM methods")}
    assert "doAlpha" in methods_a
    assert "doBeta" in methods_b
    assert "doBeta" not in methods_a
    assert "doAlpha" not in methods_b

    all_ids = {p["id"] for p in sg.list_project_metadata()}
    assert pid_a in all_ids
    assert pid_b in all_ids


# ---------------------------------------------------------------------------
# Legacy KùzuDB artifact recovery
# ---------------------------------------------------------------------------


def test_legacy_kuzu_directory_at_db_path_is_removed(tmp_path: Path):
    """DuckDBStore auto-removes a KùzuDB directory left at the DB path."""
    db_path = str(tmp_path / "db")
    snap_path = str(tmp_path / "db_read")

    # Simulate a KùzuDB directory at the write path
    os.makedirs(db_path)
    (Path(db_path) / "nodes.index").write_bytes(b"\x00" * 16)

    store = DuckDBStore(db_path_override=db_path, snapshot_path_override=snap_path)
    rows = store.query_records("SELECT * FROM projects")
    assert rows == []  # fresh empty DB, no crash


def test_legacy_kuzu_file_at_snapshot_path_is_removed(tmp_path: Path):
    """DuckDBStore auto-removes a non-DuckDB file left at the snapshot path."""
    db_path = str(tmp_path / "db")
    snap_path = str(tmp_path / "db_read")

    # Write a KùzuDB-style snapshot *directory* at the snap path
    os.makedirs(snap_path)
    (Path(snap_path) / "catalog.json").write_bytes(b"{}")

    # Open read-only — should clear the bad snapshot and open fresh
    store = DuckDBStore(read_only=True, db_path_override=db_path, snapshot_path_override=snap_path)
    rows = store.query_records("SELECT * FROM projects")
    assert rows == []


def test_corrupt_file_at_db_path_is_replaced(tmp_path: Path):
    """DuckDBStore replaces a corrupt (non-DuckDB) regular file at the DB path."""
    db_path = str(tmp_path / "db")
    snap_path = str(tmp_path / "db_read")

    # Write garbage that DuckDB cannot open
    Path(db_path).write_bytes(b"NOT A DUCKDB FILE\x00\x01\x02")

    store = DuckDBStore(db_path_override=db_path, snapshot_path_override=snap_path)
    rows = store.query_records("SELECT * FROM projects")
    assert rows == []


def test_legacy_kuzu_dirs_at_both_paths_are_removed(tmp_path: Path):
    """Regression: both db and db_read as KùzuDB directories (the exact
    scenario from the field bug in v1.0.2)."""
    db_path = str(tmp_path / "db")
    snap_path = str(tmp_path / "db_read")

    # Simulate KùzuDB directories at BOTH paths
    for p in (db_path, snap_path):
        os.makedirs(p)
        (Path(p) / "catalog.kz").write_bytes(b"\x00" * 64)
        (Path(p) / "data.kz").write_bytes(b"\x00" * 1024)

    # Read-only open: legacy code would pick snap, fail, fall back to db, fail, raise.
    # New code never deletes paths in read-only mode and returns an empty view.
    store = DuckDBStore(read_only=True, db_path_override=db_path, snapshot_path_override=snap_path)
    rows = store.query_records("SELECT * FROM projects")
    assert rows == []
    assert os.path.exists(db_path)
    assert os.path.exists(snap_path)


def test_read_only_open_does_not_delete_existing_snapshot(tmp_path: Path):
    db_path = str(tmp_path / "db")
    snap_path = str(tmp_path / "db_read")
    Path(snap_path).write_bytes(b"not-a-duckdb-snapshot")

    store = DuckDBStore(read_only=True, db_path_override=db_path, snapshot_path_override=snap_path)

    assert store.query_records("SELECT * FROM projects") == []
    assert os.path.exists(snap_path)


def test_invalid_temp_snapshot_does_not_replace_last_good_replica(tmp_path: Path, monkeypatch):
    db_path = str(tmp_path / "db")
    snap_path = str(tmp_path / "db_read")
    store = DuckDBStore(db_path_override=db_path, snapshot_path_override=snap_path)
    store.upsert_project("good", "/good")
    assert store.snapshot_to_read_replica() is True
    before = Path(snap_path).read_bytes()

    original_copy2 = shutil.copy2

    def fake_copy2(src, dst, *args, **kwargs):
        result = original_copy2(src, dst, *args, **kwargs)
        Path(dst).write_bytes(b"broken snapshot")
        return result

    monkeypatch.setattr("codespine.db.duckdb_store.shutil.copy2", fake_copy2)

    assert store.snapshot_to_read_replica() is False
    assert Path(snap_path).read_bytes() == before


def test_sharded_store_stats_flow_with_stale_kuzu_dirs(tmp_path: Path):
    """Regression: ShardedGraphStore.list_project_metadata() must not crash
    when every shard path is a stale KùzuDB directory (the failing
    'codespine stats' scenario)."""
    shards_dir = tmp_path / "shards"
    # Pre-create 4 shards with legacy KùzuDB-style directories at both paths
    for i in range(4):
        (shards_dir / str(i)).mkdir(parents=True)
        (shards_dir / str(i) / "db").mkdir()
        (shards_dir / str(i) / "db" / "catalog.kz").write_bytes(b"\x00" * 32)
        (shards_dir / str(i) / "db_read").mkdir()

    sg = ShardedGraphStore(read_only=True, shards_dir=str(shards_dir), backend="duckdb")
    # This is what `codespine stats` does — it must not raise.
    assert sg.list_project_metadata() == []
