from __future__ import annotations

import os
from pathlib import Path

from codespine.mcp.server import _reload_store_instance, _store_snapshot_mtime


class _FakeRouter:
    def __init__(self, base: Path):
        self._base = base

    def all_shards(self):
        return [0, 1]

    def shard_for(self, project_id: str) -> int:
        return 1 if project_id == "project-b" else 0

    def snapshot_path(self, idx: int) -> str:
        return str(self._base / str(idx) / "db_read")


class _FakeShardedStore:
    def __init__(self, read_only: bool = False, num_shards: int | None = None, shards_dir: str | None = None, backend: str | None = None):
        self.read_only = read_only
        self.backend = backend or "duckdb"
        self.router = type(
            "Router",
            (),
            {
                "num_shards": num_shards,
                "shards_dir": shards_dir,
                "all_shards": lambda self: [0, 1],
                "shard_for": lambda self, project_id: 1 if project_id == "project-b" else 0,
                "snapshot_path": lambda self, idx: str(Path(shards_dir) / str(idx) / "db_read"),
            },
        )()


class _FakeDuckStore:
    def __init__(self, read_only: bool = False, db_path_override: str | None = None, snapshot_path_override: str | None = None):
        self.read_only = read_only
        self._db_path = db_path_override or "db"
        self._snapshot_path = snapshot_path_override or "db_read"


def test_store_snapshot_mtime_tracks_sharded_snapshots(tmp_path: Path):
    shard0 = tmp_path / "0" / "db_read.updated"
    shard1 = tmp_path / "1" / "db_read.updated"
    shard0.parent.mkdir(parents=True)
    shard1.parent.mkdir(parents=True)
    shard0.write_text("old", encoding="utf-8")
    shard1.write_text("new", encoding="utf-8")
    os.utime(shard0, (1_000.0, 1_000.0))
    os.utime(shard1, (2_000.0, 2_000.0))

    store = type("Store", (), {"router": _FakeRouter(tmp_path)})()

    assert _store_snapshot_mtime(store) == 2_000.0
    assert _store_snapshot_mtime(store, project="project-b") == 2_000.0


def test_reload_store_preserves_backend_specific_type(tmp_path: Path):
    sharded = _FakeShardedStore(read_only=False, num_shards=4, shards_dir=str(tmp_path / "shards"), backend="duckdb")
    reloaded_sharded = _reload_store_instance(sharded)
    assert isinstance(reloaded_sharded, _FakeShardedStore)
    assert reloaded_sharded.read_only is True
    assert reloaded_sharded.backend == "duckdb"
    assert reloaded_sharded.router.num_shards == 4
    assert reloaded_sharded.router.shards_dir == str(tmp_path / "shards")

    duck = _FakeDuckStore(read_only=False, db_path_override=str(tmp_path / "db"), snapshot_path_override=str(tmp_path / "db_read"))
    reloaded_duck = _reload_store_instance(duck)
    assert isinstance(reloaded_duck, _FakeDuckStore)
    assert reloaded_duck.read_only is True
    assert reloaded_duck._db_path == str(tmp_path / "db")
    assert reloaded_duck._snapshot_path == str(tmp_path / "db_read")
