from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

from codespine.indexer.engine import JavaIndexer
from codespine.mcp.server import build_mcp_server
from codespine.overlay.merge import merged_class_records
from codespine.sharding.store import ShardedGraphStore


class _OverlaylessStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "MATCH (c:Class), (f:File)" in query:
            return [
                {
                    "id": "c1",
                    "name": "TransactionService",
                    "fqcn": "com.example.TransactionService",
                    "package": "com.example",
                    "file_id": "f1",
                    "project_id": "app",
                    "file_path": "/tmp/TransactionService.java",
                }
            ]
        if "MATCH (m:Method), (c:Class), (f:File)" in query:
            return []
        if "s.kind = 'field'" in query:
            return []
        if "MATCH (p:Project)" in query:
            return []
        return []


def test_merged_class_records_handles_missing_overlay_store() -> None:
    rows = merged_class_records(_OverlaylessStore(), None)
    assert len(rows) == 1
    assert rows[0]["name"] == "TransactionService"


def test_sharded_store_overlay_store_available_in_empty_read_only_mode(tmp_path: Path) -> None:
    sg = ShardedGraphStore(read_only=True, num_shards=1, shards_dir=str(tmp_path / "shards"), backend="duckdb")
    overlay_store = sg.overlay_store
    assert overlay_store is not None
    assert hasattr(overlay_store, "list_projects")


def test_detect_projects_in_workspace_promotes_module_path_to_reactor_root(tmp_path: Path) -> None:
    root = tmp_path / "vision"
    module = root / "vision-server"
    module.mkdir(parents=True)
    (root / "pom.xml").write_text("<project><modules><module>vision-server</module></modules></project>", encoding="utf-8")
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")

    projects = JavaIndexer.detect_projects_in_workspace(str(module))
    assert projects == [str(root.resolve())]


def test_supervisor_uses_http_transport_for_background_daemon() -> None:
    import codespine.cli as cli

    source = inspect.getsource(cli.main.commands["supervise-mcp"].callback)
    assert "streamable-http" in source
    assert "--transport" in source


def test_find_symbol_handles_missing_overlay_store_without_none_regression() -> None:
    async def _run():
        mcp = build_mcp_server(_OverlaylessStore(), lambda: ".")
        result = await mcp.call_tool("find_symbol", {"name": "TransactionService"})
        payload = json.loads(result.content[0].text)
        assert payload["available"] is True
        assert payload["by_project"]["app"]["classes"][0]["name"] == "TransactionService"

    asyncio.run(_run())
