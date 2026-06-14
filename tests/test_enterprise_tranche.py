from __future__ import annotations

import asyncio
import json

import pytest

from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact, resolve_symbol_targets
from codespine.mcp.server import build_mcp_server


def test_flow_truncation_metadata_is_truthful_and_default_api_stays_list(monkeypatch):
    class _Store:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (a:Method)-[:CALLS]->(b:Method)" in query:
                return [
                    {"src": "e1", "dst": "n1"},
                    {"src": "n1", "dst": "n2"},
                    {"src": "e2", "dst": "m1"},
                ]
            raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("codespine.analysis.flow._entry_methods", lambda *args, **kwargs: ["e1", "e2", "e3"])
    monkeypatch.setattr("codespine.analysis.flow._resolve_entry_methods", lambda *args, **kwargs: ["e1", "e2", "e3"])
    monkeypatch.setattr(
        "codespine.analysis.flow._resolve_method_metadata",
        lambda *args, **kwargs: {
            "e1": {"name": "main", "fqname": "App#main", "file_path": "/repo/App.java", "project_id": "app"},
            "n1": {"name": "one", "fqname": "App#one", "file_path": "/repo/App.java", "project_id": "app"},
            "n2": {"name": "two", "fqname": "App#two", "file_path": "/repo/App.java", "project_id": "app"},
            "e2": {"name": "alt", "fqname": "App#alt", "file_path": "/repo/Alt.java", "project_id": "app"},
            "m1": {"name": "leaf", "fqname": "App#leaf", "file_path": "/repo/Alt.java", "project_id": "app"},
            "e3": {"name": "other", "fqname": "App#other", "file_path": "/repo/Other.java", "project_id": "app"},
        },
    )

    default_result = trace_execution_flows(_Store(), project="app")
    assert isinstance(default_result, list)
    assert len(default_result) == 3

    limited = trace_execution_flows(
        _Store(),
        project="app",
        include_metadata=True,
        entry_point_cap=1,
        per_flow_node_cap=2,
        total_node_cap=2,
    )

    assert limited["truncation"] == {
        "entry_point_cap": 1,
        "per_flow_node_cap": 2,
        "total_node_cap": 2,
        "entry_points_found": 3,
        "entry_points_emitted": 1,
        "entry_points_truncated": True,
        "total_nodes_emitted": 2,
        "total_node_cap_reached": False,
        "flows_truncated": True,
    }
    assert [node["name"] for node in limited["flows"][0]["nodes"]] == ["main", "one"]
    assert limited["flows"][0]["truncated"] is True


def test_mcp_flow_tool_routes_through_metadata_path(monkeypatch):
    class _Store:
        overlay_store = None

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            return []

    captured: dict[str, object] = {}

    def fake_trace_flows(*args, **kwargs):
        captured.update(kwargs)
        return {
            "flows": [{"entry": "m1", "nodes": [{"symbol": "m1", "depth": 0, "name": "main"}], "truncated": False}],
            "truncation": {"entry_point_cap": 1, "per_flow_node_cap": 1, "total_node_cap": 1},
        }

    monkeypatch.setattr("codespine.mcp.server.trace_flows_analysis", fake_trace_flows)

    async def _run() -> None:
        mcp = build_mcp_server(_Store(), lambda: ".")
        result = await mcp.call_tool("trace_execution_flows", {"entry_symbol": "Foo", "project": "app"})
        payload = json.loads(result.content[0].text)
        assert captured["include_metadata"] is True
        assert payload["flow_truncation"]["entry_point_cap"] == 1

    asyncio.run(_run())


def test_impact_resolution_stays_project_scoped_and_ambiguous_queries_do_not_merge():
    class _ExactStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (s:Symbol)" in query and "f.project_id = $proj" in query:
                return [
                    {
                        "id": "s_target",
                        "kind": "method",
                        "name": "target",
                        "fqname": "com.example.App#target()",
                        "file_id": "f_app",
                        "project_id": "app",
                        "file_path": "/repo/app/App.java",
                    }
                ]
            if "MATCH (m:Method), (c:Class), (f:File)" in query and "lower(c.fqcn) = lower($class_fqcn)" in query:
                if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                    return [
                        {
                            "id": "m_target",
                            "name": "target",
                            "fqname": "target()",
                            "class_fqcn": "com.example.App",
                            "project_id": "app",
                            "file_path": "/repo/app/App.java",
                        }
                    ]
                return [{"id": "m_target"}]
            if "CALLS" in query and "fa.project_id = $proj" in query:
                return [
                    {"src": "m_caller", "dst": "m_target", "edge_type": "CALLS", "confidence": 0.9, "reason": "project"}
                ]
            if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                ids = params.get("ids", []) if params else []
                meta = {
                    "m_target": {"id": "m_target", "name": "target", "fqname": "target()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"},
                    "m_caller": {"id": "m_caller", "name": "caller", "fqname": "caller()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"},
                }
                return [meta[mid] for mid in ids if mid in meta]
            return []

    class _AmbiguousStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (s:Symbol)" in query and "f.project_id = $proj" in query:
                return [
                    {"id": "s1", "kind": "method", "name": "target", "fqname": "com.example.App#target()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java"},
                    {"id": "s2", "kind": "method", "name": "target", "fqname": "com.example.Other#target()", "file_id": "f_other", "project_id": "app", "file_path": "/repo/app/Other.java"},
                ]
            return []

    exact = analyze_impact(_ExactStore(), "target", project="app")
    assert exact["resolution"]["status"] == "exact"
    assert [item["project_id"] for item in exact["resolved_to"]] == ["app"]
    assert any(item["project_id"] == "app" for item in exact["self_callers"] + exact["impacted_callers"]["1"])

    ambiguous = analyze_impact(_AmbiguousStore(), "target", project="app")
    assert ambiguous["resolution"]["status"] == "ambiguous"
    assert ambiguous["depth_groups"] == {"1": [], "2": [], "3+": []}
    assert resolve_symbol_targets(_AmbiguousStore(), "target", project="app")["status"] == "ambiguous"


def test_what_breaks_and_explain_use_backend_safe_resolution(monkeypatch):
    class _Store:
        overlay_store = None

        def __init__(self) -> None:
            self.queries: list[str] = []

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            self.queries.append(query)
            assert "CONTAINS" not in query
            if "IN_COMMUNITY" in query:
                return [{"id": "c1", "label": "pkg", "cohesion": 0.9}]
            if "caller:Method" in query:
                return [{"id": "m_caller", "name": "caller"}]
            if "callee:Method" in query:
                return [{"id": "m_callee", "name": "callee"}]
            if "MATCH (m:Method), (c:Class), (f:File)" in query and "lower(c.fqcn) = lower($class_fqcn)" in query:
                if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                    return [
                        {"id": "m_target", "name": "target", "fqname": "target()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"}
                    ]
                return [{"id": "m_target"}]
            if "MATCH (s:Symbol)" in query:
                return [
                    {"id": "s_target", "kind": "method", "name": "target", "fqname": "com.example.App#target()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java"}
                ]
            if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                ids = params.get("ids", []) if params else []
                meta = {
                    "m_target": {"id": "m_target", "name": "target", "fqname": "target()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"},
                    "m_caller": {"id": "m_caller", "name": "caller", "fqname": "caller()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"},
                    "m_callee": {"id": "m_callee", "name": "callee", "fqname": "callee()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"},
                }
                return [meta[mid] for mid in ids if mid in meta]
            return []

    async def _run() -> None:
        store = _Store()
        mcp = build_mcp_server(store, lambda: ".")
        explain = await mcp.call_tool("explain", {"symbol": "target", "project": "app"})
        what_breaks = await mcp.call_tool("what_breaks", {"symbol": "target", "project": "app"})
        explain_payload = json.loads(explain.content[0].text)
        breaks_payload = json.loads(what_breaks.content[0].text)
        assert explain_payload["available"] is True
        assert explain_payload["matched"][0]["id"] == "s_target"
        assert breaks_payload["available"] is True
        assert breaks_payload["risk_level"] == "low"
        assert any("MATCH (s:Symbol)" in q and "f.project_id = $proj" in q for q in store.queries)
        assert any("MATCH (m:Method), (c:Class), (f:File)" in q and "f.project_id = $proj" in q for q in store.queries)
        assert any("caller:Method" in q and "fa.project_id = $proj" in q for q in store.queries)
        assert all("CONTAINS" not in q for q in store.queries)

    asyncio.run(_run())


def test_what_breaks_and_explain_prefer_fqcn_input_over_bare_name_hit(monkeypatch):
    class _Store:
        overlay_store = None

        def __init__(self) -> None:
            self.seen: list[str] = []

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            q = (params or {}).get("q")
            sid = (params or {}).get("sid")
            self.seen.append(str(q or sid or query))
            if "MATCH (s:Symbol)" in query:
                if q == "com.example.App#foo()":
                    return [
                        {"id": "s_foo", "kind": "method", "name": "foo", "fqname": "com.example.App#foo()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java"},
                    ]
                if q == "foo()":
                    return [
                        {"id": "s_shadow", "kind": "method", "name": "foo", "fqname": "foo()", "file_id": "f_shadow", "project_id": "app", "file_path": "/repo/app/Shadow.java"},
                    ]
            if "MATCH (m:Method), (c:Class), (f:File)" in query and "lower(c.fqcn) = lower($class_fqcn)" in query:
                if (params or {}).get("class_fqcn") == "com.example.App" and (params or {}).get("signature") == "foo()":
                    return [{"id": "m_foo", "name": "foo", "fqname": "foo()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"}]
                return []
            if "MATCH (s:Symbol), (m:Method), (c:Class), (f:File)" in query and "s.id = $sid" in query:
                if sid == "s_shadow":
                    return [{"sid": "s_shadow", "mid": "m_shadow"}]
            if "caller:Method" in query or "callee:Method" in query:
                return []
            if "IN_COMMUNITY" in query:
                return []
            if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                ids = params.get("ids", []) if params else []
                meta = {
                    "m_shadow": {"id": "m_shadow", "name": "foo", "fqname": "foo()", "class_fqcn": "com.example.Other", "project_id": "app", "file_path": "/repo/app/Shadow.java"},
                    "m_foo": {"id": "m_foo", "name": "foo", "fqname": "foo()", "class_fqcn": "com.example.App", "project_id": "app", "file_path": "/repo/app/App.java"},
                }
                return [meta[mid] for mid in ids if mid in meta]
            return []

    async def _run() -> None:
        store = _Store()
        mcp = build_mcp_server(store, lambda: ".")
        explain = await mcp.call_tool("explain", {"symbol": "com.example.App#foo()", "project": "app"})
        what_breaks = await mcp.call_tool("what_breaks", {"symbol": "com.example.App#foo()", "project": "app"})
        explain_payload = json.loads(explain.content[0].text)
        breaks_payload = json.loads(what_breaks.content[0].text)
        assert explain_payload["available"] is True
        assert breaks_payload["available"] is True
        assert explain_payload["matched"][0]["id"] == "s_foo"
        assert breaks_payload["resolved_to"][0]["id"] == "m_foo"
        assert "com.example.App#foo()" in store.seen
        assert "foo()" not in store.seen

    asyncio.run(_run())


def test_flow_truncation_marks_total_cap_limited_entry_points(monkeypatch):
    class _Store:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (a:Method)-[:CALLS]->(b:Method)" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("codespine.analysis.flow._entry_methods", lambda *args, **kwargs: ["e1", "e2", "e3"])
    monkeypatch.setattr("codespine.analysis.flow._resolve_method_metadata", lambda *args, **kwargs: {})

    result = trace_execution_flows(
        _Store(),
        project="app",
        include_metadata=True,
        entry_point_cap=10,
        per_flow_node_cap=10,
        total_node_cap=2,
    )

    assert result["truncation"] == {
        "entry_point_cap": 10,
        "per_flow_node_cap": 10,
        "total_node_cap": 2,
        "entry_points_found": 3,
        "entry_points_emitted": 2,
        "entry_points_truncated": True,
        "total_nodes_emitted": 2,
        "total_node_cap_reached": True,
        "flows_truncated": True,
    }
    assert [flow["entry"] for flow in result["flows"]] == ["e1", "e2"]
    assert all(flow["truncated"] is False for flow in result["flows"])


def test_limited_flow_tracer_skips_empty_flows_when_entry_cannot_be_captured(monkeypatch):
    class _Store:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (a:Method)-[:CALLS]->(b:Method)" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr("codespine.analysis.flow._entry_methods", lambda *args, **kwargs: ["e1"])
    monkeypatch.setattr("codespine.analysis.flow._resolve_method_metadata", lambda *args, **kwargs: {})

    result = trace_execution_flows(
        _Store(),
        project="app",
        include_metadata=True,
        entry_point_cap=10,
        per_flow_node_cap=0,
        total_node_cap=10,
    )

    assert result["flows"] == []
    assert result["truncation"]["entry_points_emitted"] == 0
    assert result["truncation"]["flows_truncated"] is True


def test_what_breaks_ambiguous_resolution_reports_unknown_risk(monkeypatch):
    class _Store:
        overlay_store = None

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            return []

    monkeypatch.setattr(
        "codespine.mcp.server.analyze_impact",
        lambda *args, **kwargs: {
            "target": "Foo",
            "resolution": {"status": "ambiguous", "matches": [{"id": "s1"}, {"id": "s2"}], "resolved_method_ids": []},
            "ambiguity": {"matches": [{"id": "s1"}, {"id": "s2"}]},
            "resolved_to": [],
            "impacted_callers": {"1": [], "2": [], "3+": []},
            "self_callers": [],
            "summary": {"direct": 0, "indirect": 0, "transitive": 0, "self_callers": 0},
        },
    )

    async def _run() -> None:
        mcp = build_mcp_server(_Store(), lambda: ".")
        result = await mcp.call_tool("what_breaks", {"symbol": "Foo", "project": "app"})
        payload = json.loads(result.content[0].text)
        assert payload["available"] is True
        assert payload["risk_level"] == "unknown"
        assert payload["ambiguity"]["status"] == "ambiguous"

    asyncio.run(_run())
