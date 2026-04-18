"""Tests for sharding infrastructure: ShardRouter + ShardedGraphStore."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("kuzu")
pytest.importorskip("tree_sitter_java")

from codespine.sharding.router import ShardRouter
from codespine.sharding.store import ShardedGraphStore
from codespine.indexer.engine import JavaIndexer


# ---------------------------------------------------------------------------
# ShardRouter
# ---------------------------------------------------------------------------


def test_router_co_location():
    """All modules of the same project must hash to the same shard."""
    r = ShardRouter(num_shards=8)
    root_shard = r.shard_for("myapp")
    assert r.shard_for("myapp::module-a") == root_shard
    assert r.shard_for("myapp::module-b") == root_shard
    assert r.shard_for("myapp::module-c") == root_shard


def test_router_single_shard_always_zero():
    """With num_shards=1 every project must land on shard 0."""
    r = ShardRouter(num_shards=1)
    for pid in ["alpha", "beta", "gamma::sub", "delta"]:
        assert r.shard_for(pid) == 0


def test_router_distribution(tmp_path: Path):
    """With 4 shards and many distinct projects, at least 2 shards get used."""
    r = ShardRouter(num_shards=4)
    projects = [f"project-{i}" for i in range(50)]
    used_shards = {r.shard_for(p) for p in projects}
    assert len(used_shards) >= 2, "Poor distribution — expected multiple shards to be used"


def test_router_deterministic():
    """Same project_id must always map to the same shard across instances."""
    r1 = ShardRouter(num_shards=4)
    r2 = ShardRouter(num_shards=4)
    for pid in ["foo", "bar::baz", "qux"]:
        assert r1.shard_for(pid) == r2.shard_for(pid)


def test_router_paths(tmp_path: Path):
    r = ShardRouter(num_shards=3, shards_dir=str(tmp_path / "shards"))
    assert r.db_path(0).endswith("/0/db")
    assert r.snapshot_path(1).endswith("/1/db_read")


# ---------------------------------------------------------------------------
# ShardedGraphStore — basic routing
# ---------------------------------------------------------------------------


def _write_java(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_sharded_store_routes_modules_to_same_shard(tmp_path: Path):
    """All modules of the same project must use the same GraphStore instance."""
    sg = ShardedGraphStore(num_shards=4, shards_dir=str(tmp_path / "shards"))
    store_a = sg.shard("myapp::module-a")
    store_b = sg.shard("myapp::module-b")
    store_c = sg.shard("myapp::module-c")
    assert store_a is store_b
    assert store_b is store_c
    assert store_a._db_path == store_b._db_path


def test_sharded_store_different_projects_may_differ(tmp_path: Path):
    """Two unrelated projects may (and likely will) land on different stores."""
    sg = ShardedGraphStore(num_shards=8, shards_dir=str(tmp_path / "shards"))
    # Find two project IDs that hash to different shards.
    router = sg.router
    p1, p2 = None, None
    for i in range(200):
        pid = f"project-{i}"
        if p1 is None:
            p1 = pid
        elif router.shard_for(pid) != router.shard_for(p1):
            p2 = pid
            break
    if p2 is None:
        pytest.skip("All 200 projects happened to hash to the same shard — skip")
    store_1 = sg.shard(p1)
    store_2 = sg.shard(p2)
    assert store_1 is not store_2
    assert store_1._db_path != store_2._db_path


def test_sharded_store_list_projects_empty(tmp_path: Path):
    sg = ShardedGraphStore(read_only=False, num_shards=2, shards_dir=str(tmp_path / "shards"))
    # Force open all shards by reading — they're empty so result should be []
    projects = sg.list_project_metadata()
    assert projects == []


# ---------------------------------------------------------------------------
# Indexing via ShardedGraphStore
# ---------------------------------------------------------------------------


def test_index_single_project_via_sharded_store(tmp_path: Path):
    """Indexing through ShardedGraphStore should produce queryable results."""
    _write_java(
        tmp_path / "src/main/java/com/example/Hello.java",
        """
        package com.example;
        public class Hello {
            public String greet(String name) { return "Hi " + name; }
        }
        """,
    )

    sg = ShardedGraphStore(num_shards=2, shards_dir=str(tmp_path / "shards"))
    project_id = "hello-project"
    shard_store = sg.shard(project_id)
    result = JavaIndexer(shard_store).index_project(
        str(tmp_path), full=True, project_id=project_id
    )

    assert result.files_indexed == 1
    assert result.classes_indexed >= 1
    assert result.methods_indexed >= 1

    classes = shard_store.query_records(
        "MATCH (c:Class) WHERE c.fqcn = $fqcn RETURN c.name as name",
        {"fqcn": "com.example.Hello"},
    )
    assert classes, "Class not found in shard DB"
    assert classes[0]["name"] == "Hello"

    # list_project_metadata fan-out should find the project.
    all_projects = sg.list_project_metadata()
    assert any(p["id"] == project_id for p in all_projects)


def test_two_projects_indexed_into_separate_shards(tmp_path: Path):
    """When two projects land on different shards they're stored independently."""
    _write_java(
        tmp_path / "proj-a" / "src" / "main" / "java" / "a" / "A.java",
        "package a; public class A { public void alpha() {} }",
    )
    _write_java(
        tmp_path / "proj-b" / "src" / "main" / "java" / "b" / "B.java",
        "package b; public class B { public void beta() {} }",
    )

    sg = ShardedGraphStore(num_shards=8, shards_dir=str(tmp_path / "shards"))
    router = sg.router

    # Find project IDs that will use different shards.
    pid_a = "proj-a"
    pid_b = None
    for candidate in [f"project-alt-{i}" for i in range(100)]:
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

    # Each shard should contain exactly one project.
    store_a = sg.shard(pid_a)
    store_b = sg.shard(pid_b)
    assert store_a is not store_b

    # Methods are visible only in their owning shard.
    methods_a = store_a.query_records("MATCH (m:Method) RETURN m.name as name")
    methods_b = store_b.query_records("MATCH (m:Method) RETURN m.name as name")
    names_a = {m["name"] for m in methods_a}
    names_b = {m["name"] for m in methods_b}
    assert "alpha" in names_a
    assert "beta" in names_b
    # Cross-shard isolation: alpha not in shard B, beta not in shard A.
    assert "beta" not in names_a
    assert "alpha" not in names_b

    # Fan-out list should see both.
    all_projects = sg.list_project_metadata()
    all_ids = {p["id"] for p in all_projects}
    assert pid_a in all_ids
    assert pid_b in all_ids
