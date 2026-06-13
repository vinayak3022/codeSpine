from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from codespine.analysis.context import build_symbol_context
from codespine.cli import main
from codespine.mcp.server import build_mcp_server
from codespine.search.hybrid import hybrid_search
from codespine.search.vector import embed_text


class _FailingContextStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "RETURN s.id as id" in query:
            return [
                {
                    "id": "s1",
                    "kind": "class",
                    "name": "Foo",
                    "fqname": "com.example.Foo",
                    "embedding": None,
                    "line": 1,
                    "file_id": "f1",
                    "file_path": "/tmp/Foo.java",
                    "project_id": "app",
                    "is_test": False,
                }
            ]
        if "IN_COMMUNITY" in query or "IN_FLOW" in query:
            raise RuntimeError("context exploded")
        return []


class _ExplainableStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "RETURN s.id as id" in query:
            return [
                {
                    "id": "s1",
                    "kind": "class",
                    "name": "Foo",
                    "fqname": "com.example.Foo",
                    "embedding": embed_text("Foo"),
                    "line": 1,
                    "file_id": "f1",
                    "file_path": "/tmp/Foo.java",
                    "project_id": "app",
                    "is_test": False,
                }
            ]
        if "IN_COMMUNITY" in query or "IN_FLOW" in query or "MATCH (p:Project)" in query:
            return []
        return []


class _LowConfidenceExplainableStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "RETURN s.id as id" in query:
            return [
                {
                    "id": "s1",
                    "kind": "class",
                    "name": "Bar",
                    "fqname": "com.example.Bar",
                    "embedding": None,
                    "line": 1,
                    "file_id": "f1",
                    "file_path": "/tmp/Bar.java",
                    "project_id": "app",
                    "is_test": False,
                }
            ]
        if "IN_COMMUNITY" in query or "IN_FLOW" in query or "MATCH (p:Project)" in query:
            return []
        return []


def test_hybrid_search_degrades_gracefully_when_context_lookup_fails():
    results = hybrid_search(_FailingContextStore(), "Foo", k=1)

    assert len(results) == 1
    assert results[0]["name"] == "Foo"
    assert results[0]["context"] == []
    assert results[0]["context_warning"] == "Architectural context unavailable for this result."


def test_build_symbol_context_keeps_search_candidates_when_context_lookup_fails(monkeypatch):
    monkeypatch.setattr("codespine.analysis.context.analyze_impact", lambda *args, **kwargs: {"resolved_to": []})
    monkeypatch.setattr("codespine.analysis.context.symbol_community", lambda *args, **kwargs: {"matches": []})
    monkeypatch.setattr("codespine.analysis.context.trace_execution_flows", lambda *args, **kwargs: [])

    result = build_symbol_context(_FailingContextStore(), "Foo")

    assert result["focus"]["name"] == "Foo"
    assert len(result["search_candidates"]) == 1
    assert result["search_candidates"][0]["context"] == []
    assert "context_warning" in result["search_candidates"][0]


def test_hybrid_search_returns_flow_depth_from_duckdb_context(tmp_path: Path):
    pytest.importorskip("duckdb")

    from codespine.db.duckdb_store import DuckDBStore

    store = DuckDBStore(
        db_path_override=str(tmp_path / "db"),
        snapshot_path_override=str(tmp_path / "db_read"),
    )
    store.upsert_project("app", "/app")
    store.upsert_file("f1", "/app/src/main/java/com/example/OrderService.java", "app", False, "abc")
    store.upsert_symbols_batch(
        [
            {
                "id": "s1",
                "kind": "class",
                "name": "OrderService",
                "fqname": "com.example.OrderService",
                "file_id": "f1",
                "line": 1,
                "col": 1,
                "embedding": None,
            }
        ]
    )
    store.set_community("comm1", "Ordering", 0.9, ["s1"])
    store.set_flow("flow1", "s1", "entry", [("s1", 0)])

    results = hybrid_search(store, "OrderService", k=1, project="app")

    assert len(results) == 1
    assert results[0]["name"] == "OrderService"
    assert results[0]["context"]
    assert any(item.get("community_label") == "Ordering" for item in results[0]["context"])
    assert any(item.get("flow_depth") == 0 for item in results[0]["context"])


def test_hybrid_search_context_does_not_cross_product_community_and_flow_rows():
    class _ContextStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "RETURN s.id as id" in query:
                return [
                    {
                        "id": "s1",
                        "kind": "class",
                        "name": "Foo",
                        "fqname": "com.example.Foo",
                        "embedding": embed_text("Foo"),
                        "line": 1,
                        "file_id": "f1",
                        "file_path": "/tmp/Foo.java",
                        "project_id": "app",
                        "is_test": False,
                    }
                ]
            if "IN_COMMUNITY" in query:
                return [
                    {"community_id": "comm1", "community_label": "Ordering"},
                    {"community_id": "comm2", "community_label": "Payments"},
                ]
            if "IN_FLOW" in query:
                return [
                    {"flow_id": "flow1", "flow_kind": "entry", "flow_depth": 0},
                    {"flow_id": "flow2", "flow_kind": "exit", "flow_depth": 1},
                ]
            return []

    results = hybrid_search(_ContextStore(), "Foo", k=1, project="app")

    assert len(results) == 1
    context = results[0]["context"]
    assert len(context) == 3
    assert sum(1 for item in context if item.get("community_id") and not item.get("flow_id")) == 2
    assert sum(1 for item in context if item.get("flow_id") and not item.get("community_id")) == 1
    assert all(not (item.get("community_id") and item.get("flow_id")) for item in context)


def test_hybrid_search_explain_returns_provenance_envelope():
    results = hybrid_search(_ExplainableStore(), "Foo", k=1, explain=True)

    assert results["retrieval_mode"] == "hybrid"
    assert results["query"] == "Foo"
    assert results["results"][0]["confidence"] == "high"
    assert results["results"][0]["confidence_reason"] == "Exact name match"
    assert "exact name match" in results["results"][0]["match_reasons"]
    assert results["results"][0]["retrieval_traces"]["bm25"]["rank"] == 1
    assert results["results"][0]["retrieval_traces"]["semantic"]["rank"] == 1
    assert set(results["provenance"]["rankers"].keys()) == {"bm25", "semantic", "fuzzy"}


def test_hybrid_search_explain_keeps_low_confidence_note_outside_results_array():
    results = hybrid_search(_LowConfidenceExplainableStore(), "Foo", k=1, explain=True)

    assert results["results"][0]["confidence"] == "low"
    assert results["results"][0]["low_confidence"] is True
    assert "note" not in results["results"][0]
    assert results["note"].startswith("Low confidence results")


def test_cli_search_supports_project_and_explain(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr("codespine.cli._open_store", lambda read_only=True: object())

    def fake_hybrid_search(store, query: str, k: int = 20, project: str | None = None, explain: bool = False):
        captured.update({"query": query, "k": k, "project": project, "explain": explain})
        return {"retrieval_mode": "hybrid", "query": query, "results": [{"name": "Foo"}], "provenance": {"rankers": {}}}

    monkeypatch.setattr("codespine.cli.hybrid_search", fake_hybrid_search)

    result = CliRunner().invoke(main, ["search", "Foo", "--project", "app", "--explain", "--json"])

    assert result.exit_code == 0
    assert captured == {"query": "Foo", "k": 20, "project": "app", "explain": True}
    payload = json.loads(result.output)
    assert payload["retrieval_mode"] == "hybrid"


def test_mcp_search_hybrid_explain_flag_is_exposed_and_forwarded(monkeypatch):
    captured: dict[str, object] = {}

    class _NoopStore:
        def query_records(self, *args, **kwargs):
            return []

    def fake_hybrid_search(store, query: str, k: int = 20, project: str | None = None, explain: bool = False):
        captured.update({"query": query, "k": k, "project": project, "explain": explain})
        return {"retrieval_mode": "hybrid", "results": [{"name": "Foo"}]}

    monkeypatch.setattr("codespine.mcp.server.hybrid_search", fake_hybrid_search)

    async def _run():
        mcp = build_mcp_server(_NoopStore(), lambda: ".")
        tools = await mcp.list_tools()
        search_tool = next(tool for tool in tools if tool.name == "search_hybrid")
        assert "explain" in search_tool.parameters["properties"]
        assert search_tool.parameters["properties"]["explain"]["default"] is False
        await mcp.call_tool("search_hybrid", {"query": "Foo", "project": "app", "explain": True})

    asyncio.run(_run())

    assert captured == {"query": "Foo", "k": 20, "project": "app", "explain": True}
