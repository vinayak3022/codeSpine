from __future__ import annotations

from pathlib import Path

import pytest

from codespine.analysis.context import build_symbol_context
from codespine.search.hybrid import hybrid_search


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
