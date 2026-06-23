from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

from codespine.mcp.server import build_mcp_server, _index_guard, _reload_store_instance, _store_snapshot_mtime


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


class _FakeProc:
    pid = 4321

    def poll(self):
        return None


class _SyncThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


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


def test_build_mcp_server_auto_start_watch_uses_cli_watch_flags(monkeypatch, tmp_path: Path):
    watch_path = tmp_path / "repo"
    watch_path.mkdir()
    captured: dict[str, object] = {}

    class _Store:
        def query_records(self, query: str, params: dict | None = None):
            if "MATCH (p:Project) RETURN p.path as path, p.id as id ORDER BY p.indexed_at DESC LIMIT 1" in query:
                return [{"path": str(watch_path), "id": "project-1"}]
            return []

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr("threading.Thread", _SyncThread)
    monkeypatch.setattr("codespine.mcp.server.subprocess.Popen", fake_popen)

    build_mcp_server(_Store(), lambda: str(tmp_path))

    assert captured["cmd"] == [
        os.sys.executable,
        "-m",
        "codespine.cli",
        "watch",
        "--path",
        str(watch_path),
        "--global-interval",
        "30",
        "--overlay-debounce-ms",
        "1500",
        "--promote-on-commit",
    ]


def test_build_mcp_server_auto_start_watch_is_idempotent(monkeypatch, tmp_path: Path):
    watch_path = tmp_path / "repo"
    watch_path.mkdir()
    captured: dict[str, object] = {"count": 0}
    real_thread = threading.Thread

    class _Store:
        def query_records(self, query: str, params: dict | None = None):
            if "MATCH (p:Project) RETURN p.path as path, p.id as id ORDER BY p.indexed_at DESC LIMIT 1" in query:
                time.sleep(0.05)
                return [{"path": str(watch_path), "id": "project-1"}]
            return []

    def fake_popen(cmd, **kwargs):
        captured["count"] = int(captured["count"]) + 1
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeProc()

    class _ConcurrentStartThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target

        def start(self):
            if self._target is not None:
                threads = [real_thread(target=self._target) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

    monkeypatch.setattr("threading.Thread", _ConcurrentStartThread)
    monkeypatch.setattr("codespine.mcp.server.subprocess.Popen", fake_popen)

    build_mcp_server(_Store(), lambda: str(tmp_path))

    assert captured["count"] == 1


def test_get_impact_cache_invalidates_on_overlay_changes(monkeypatch, tmp_path: Path):
    overlay_path = tmp_path / "overlay" / "project.json"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text("{}", encoding="utf-8")
    os.utime(overlay_path, (11.1, 11.1))

    calls = {"count": 0}

    class _OverlayStore:
        def project_path(self, project_id: str) -> str:
            return str(overlay_path)

        def load_project(self, project_id: str):
            return {"project_id": project_id, "project_path": str(tmp_path), "dirty_files": {}, "deleted_files": []}

        def list_projects(self):
            return [{"project_id": "app"}]

    class _Store:
        overlay_store = _OverlayStore()

        def query_records(self, *args, **kwargs):
            return []

    def fake_analyze_impact(store, symbol: str, max_depth: int = 4, project: str | None = None):
        calls["count"] += 1
        return {"resolved_to": [{"id": symbol}], "target": symbol, "depth_groups": {"1": [], "2": [], "3+": []}, "summary": {"direct": 0, "indirect": 0, "transitive": 0, "self_callers": 0}}

    monkeypatch.setattr("codespine.mcp.server.analyze_impact", fake_analyze_impact)
    monkeypatch.setattr("codespine.mcp._tools_analysis.analyze_impact", fake_analyze_impact)

    async def _run():
        mcp = build_mcp_server(_Store(), lambda: str(tmp_path))
        first = await mcp.call_tool("get_impact", {"symbol": "Foo", "project": "app"})
        os.utime(overlay_path, (11.2, 11.2))
        second = await mcp.call_tool("get_impact", {"symbol": "Foo", "project": "app"})
        assert json.loads(first.content[0].text)["available"] is True
        assert json.loads(second.content[0].text)["available"] is True

    asyncio.run(_run())

    assert calls["count"] == 2


def test_index_guard_sums_sharded_count_rows():
    class _Store:
        def query_records(self, query: str, params: dict | None = None):
            if "MATCH (p:Project)" in query:
                return [{"n": 0}, {"n": 1}]
            if "MATCH (s:Symbol)" in query:
                return [{"n": 0}, {"n": 5}]
            return []

    assert _index_guard(_Store()) is None
