from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from codespine import __version__
from codespine.analysis.context import build_symbol_context, resolve_symbol_focus
from codespine.analysis.flow import _resolve_entry_methods, trace_execution_flows
from codespine.analysis.impact import analyze_impact
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


class _ExactMatchFastPathStore:
    def __init__(self):
        self.queries: list[str] = []

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(query)
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
                },
                {
                    "id": "s2",
                    "kind": "method",
                    "name": "Foo",
                    "fqname": "com.example.Other#Foo",
                    "embedding": None,
                    "line": 2,
                    "file_id": "f2",
                    "file_path": "/tmp/Other.java",
                    "project_id": "app",
                    "is_test": False,
                },
            ]
        if "IN_COMMUNITY" in query:
            return [
                {"symbol_id": "s1", "community_id": "comm1", "community_label": "Ordering"},
                {"symbol_id": "s2", "community_id": "comm2", "community_label": "Payments"},
            ]
        if "IN_FLOW" in query:
            return [
                {"symbol_id": "s1", "flow_id": "flow1", "flow_kind": "entry", "flow_depth": 0},
                {"symbol_id": "s2", "flow_id": "flow2", "flow_kind": "exit", "flow_depth": 1},
            ]
        return []


class _ExactMatchOrderingStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "RETURN s.id as id" in query:
            return [
                {
                    "id": "s-name-z",
                    "kind": "class",
                    "name": "com.example.Foo",
                    "fqname": "com.example.Zeta#Foo",
                    "embedding": None,
                    "line": 1,
                    "file_id": "f-name-z",
                    "file_path": "/tmp/Zeta.java",
                    "project_id": "app",
                    "is_test": False,
                },
                {
                    "id": "com.example.Foo",
                    "kind": "field",
                    "name": "Other",
                    "fqname": "com.example.Other#Other",
                    "embedding": None,
                    "line": 2,
                    "file_id": "f-id",
                    "file_path": "/tmp/Other.java",
                    "project_id": "app",
                    "is_test": False,
                },
                {
                    "id": "s-name-a",
                    "kind": "class",
                    "name": "com.example.Foo",
                    "fqname": "com.example.Alpha#Helper",
                    "embedding": None,
                    "line": 3,
                    "file_id": "f-name-a",
                    "file_path": "/tmp/HelperA.java",
                    "project_id": "app",
                    "is_test": False,
                },
                {
                    "id": "s-fqname",
                    "kind": "class",
                    "name": "Other",
                    "fqname": "com.example.Foo",
                    "embedding": None,
                    "line": 4,
                    "file_id": "f-fqname",
                    "file_path": "/tmp/Foo.java",
                    "project_id": "app",
                    "is_test": False,
                },
            ]
        if "IN_COMMUNITY" in query or "IN_FLOW" in query:
            return []
        return []


def test_hybrid_search_degrades_gracefully_when_context_lookup_fails():
    results = hybrid_search(_FailingContextStore(), "Foo", k=1)

    assert len(results) == 1
    assert results[0]["name"] == "Foo"
    assert results[0]["context"] == []
    assert results[0]["context_warning"] == "Architectural context unavailable for this result."


def test_build_symbol_context_keeps_search_candidates_when_context_lookup_fails(monkeypatch):
    monkeypatch.setattr("codespine.analysis.context.analyze_impact", lambda *args, **kwargs: {"resolved_to": [], "target": "com.example.Foo", "depth_groups": {"1": [], "2": [], "3+": []}})
    monkeypatch.setattr("codespine.analysis.context.symbol_community", lambda *args, **kwargs: {"matches": []})
    monkeypatch.setattr("codespine.analysis.context.trace_execution_flows", lambda *args, **kwargs: [])

    result = build_symbol_context(_FailingContextStore(), "Foo")

    assert result["focus"]["name"] == "Foo"
    assert len(result["search_candidates"]) == 1
    assert result["search_candidates"][0]["context"] == []
    assert "context_warning" in result["search_candidates"][0]


def test_build_symbol_context_uses_shared_focus_resolution(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    monkeypatch.setattr(
        "codespine.analysis.context.resolve_symbol_focus",
        lambda store, query, *, project=None, detail="full", k=10, search_candidates=None: calls.append((query, project))
        or {"query": query, "focus": {"id": "s1", "name": "Foo", "fqname": "com.example.Foo"}, "focus_symbol": "com.example.Foo", "search_candidates": [{"id": "s1", "name": "Foo", "fqname": "com.example.Foo"}], "search_ms": 0},
    )
    monkeypatch.setattr("codespine.analysis.context.analyze_impact", lambda *args, **kwargs: {"resolved_to": [], "target": "com.example.Foo", "depth_groups": {"1": [], "2": [], "3+": []}})
    monkeypatch.setattr("codespine.analysis.context.symbol_community", lambda *args, **kwargs: {"matches": []})
    monkeypatch.setattr("codespine.analysis.context.trace_execution_flows", lambda *args, **kwargs: [])

    result = build_symbol_context(object(), "Foo", project="app")

    assert calls == [("Foo", "app")]
    assert result["focus"]["name"] == "Foo"
    assert result["impact"]["target"] == "com.example.Foo"
    assert result["timings_ms"]["search"] == 1


def test_resolve_symbol_focus_reports_nonzero_search_time_for_prefetched_candidates(monkeypatch):
    monkeypatch.setattr("codespine.analysis.context.hybrid_search", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("hybrid_search should not run")))

    result = resolve_symbol_focus(
        object(),
        "Foo",
        search_candidates=[{"id": "s1", "name": "Foo", "fqname": "com.example.Foo"}],
    )

    assert result["search_ms"] == 1
    assert result["focus_symbol"] == "com.example.Foo"


def test_hybrid_search_detail_compact_skips_context_and_snippets_by_default(tmp_path: Path):
    source = tmp_path / "Foo.java"
    source.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

    class _DetailStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "RETURN s.id as id" in query:
                return [
                    {
                        "id": "s1",
                        "kind": "class",
                        "name": "Foo",
                        "fqname": "com.example.Foo",
                        "embedding": None,
                        "line": 3,
                        "file_id": "f1",
                        "file_path": str(source),
                        "project_id": "app",
                        "is_test": False,
                    }
                ]
            return []

    result = hybrid_search(_DetailStore(), "Foo", k=1, detail="compact")

    assert len(result) == 1
    assert "context" not in result[0]
    assert "snippet" not in result[0]


def test_hybrid_search_detail_compact_can_opt_in_context_and_snippets(tmp_path: Path):
    source = tmp_path / "Foo.java"
    source.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

    class _DetailStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "RETURN s.id as id" in query:
                return [
                    {
                        "id": "s1",
                        "kind": "class",
                        "name": "Foo",
                        "fqname": "com.example.Foo",
                        "embedding": None,
                        "line": 3,
                        "file_id": "f1",
                        "file_path": str(source),
                        "project_id": "app",
                        "is_test": False,
                    }
                ]
            if "IN_COMMUNITY" in query:
                return [{"symbol_id": "s1", "community_id": "comm1", "community_label": "Ordering"}]
            if "IN_FLOW" in query:
                return [{"symbol_id": "s1", "flow_id": "flow1", "flow_kind": "entry", "flow_depth": 0}]
            return []

    result = hybrid_search(_DetailStore(), "Foo", k=1, detail="compact", include_context=True, include_snippets=True)

    assert len(result) == 1
    assert result[0]["context"]
    assert result[0]["snippet"].startswith("line1")


def test_build_symbol_context_degrades_for_overlay_dirty_focus(monkeypatch):
    monkeypatch.setattr(
        "codespine.analysis.context.hybrid_search",
        lambda *args, **kwargs: [
            {
                "id": "s1",
                "kind": "class",
                "name": "Foo",
                "fqname": "com.example.Foo",
                "context": [],
                "context_warning": "Architectural context unavailable for this result.",
                "context_source": "overlay_dirty",
            }
        ],
    )
    monkeypatch.setattr("codespine.analysis.context.analyze_impact", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale impact analysis should be skipped")))
    monkeypatch.setattr("codespine.analysis.context.symbol_community", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale community analysis should be skipped")))
    monkeypatch.setattr("codespine.analysis.context.trace_execution_flows", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stale flow analysis should be skipped")))

    result = build_symbol_context(_FailingContextStore(), "Foo")

    assert result["focus"]["context_source"] == "overlay_dirty"
    assert result["impact"]["depth_groups"] == {"1": [], "2": [], "3+": []}
    assert result["community"]["matches"] == []
    assert result["flows"] == []
    assert "Overlay-dirty" in result["note"]


def test_build_symbol_context_skips_deep_analysis_without_focus(monkeypatch):
    monkeypatch.setattr("codespine.analysis.context.hybrid_search", lambda *args, **kwargs: [])
    monkeypatch.setattr("codespine.analysis.context.analyze_impact", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deep impact analysis should be skipped")))
    monkeypatch.setattr("codespine.analysis.context.symbol_community", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deep community analysis should be skipped")))
    monkeypatch.setattr("codespine.analysis.context.trace_execution_flows", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("deep flow analysis should be skipped")))

    result = build_symbol_context(_FailingContextStore(), "Foo")

    assert result["focus"] is None
    assert result["search_candidates"] == []
    assert result["impact"]["depth_groups"] == {"1": [], "2": [], "3+": []}
    assert result["community"]["matches"] == []
    assert result["flows"] == []
    assert "No usable focus" in result["note"]


def test_hybrid_search_degrades_context_for_overlay_dirty_symbols(monkeypatch):
    class _OverlayStore:
        def load_project(self, project: str):
            return {
                "project_id": project,
                "project_path": "/tmp/project",
                "dirty_files": {
                    "/tmp/Foo.java": {"file_id": "f1"},
                },
                "deleted_files": [],
            }

    class _OverlayAwareStore:
        overlay_store = _OverlayStore()

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "IN_COMMUNITY" in query or "IN_FLOW" in query:
                raise AssertionError("stale context query should be skipped for dirty overlay symbols")
            return []

    monkeypatch.setattr(
        "codespine.search.hybrid.merged_symbol_records",
        lambda *args, **kwargs: [
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
        ],
    )

    results = hybrid_search(_OverlayAwareStore(), "Foo", k=1, project="app")

    assert len(results) == 1
    assert results[0]["context"] == []
    assert results[0]["context_source"] == "overlay_dirty"
    assert "context_warning" in results[0]


def test_build_symbol_context_scopes_community_and_flow_by_project(monkeypatch):
    captured: dict[str, object] = {}

    def fake_analyze_impact(store, query: str, max_depth: int = 3, project: str | None = None):
        captured["impact_query"] = query
        captured["impact_project"] = project
        return {"resolved_to": []}

    def fake_symbol_community(store, query: str, project: str | None = None):
        captured["community_query"] = query
        captured["community_project"] = project
        return {"matches": []}

    def fake_trace_execution_flows(store, entry_symbol: str | None = None, max_depth: int = 6, project: str | None = None, progress=None, **kwargs):
        captured["flow_entry"] = entry_symbol
        captured["flow_project"] = project
        captured["flow_include_metadata"] = kwargs.get("include_metadata")
        return []

    monkeypatch.setattr("codespine.analysis.context.analyze_impact", fake_analyze_impact)
    monkeypatch.setattr("codespine.analysis.context.symbol_community", fake_symbol_community)
    monkeypatch.setattr("codespine.analysis.context.trace_execution_flows", fake_trace_execution_flows)

    result = build_symbol_context(_FailingContextStore(), "Foo", project="app")

    assert result["focus"]["name"] == "Foo"
    assert result["focus"]["fqname"] == "com.example.Foo"
    assert captured == {
        "impact_query": "com.example.Foo",
        "impact_project": "app",
        "community_query": "com.example.Foo",
        "community_project": "app",
        "flow_entry": "com.example.Foo",
        "flow_project": "app",
        "flow_include_metadata": True,
    }


def test_analyze_impact_scopes_traversal_by_project():
    class _ImpactScopeStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "RETURN s.id as id" in query:
                return [{"id": "s_target"}]
            if "RETURN s.id as sid, m.id as mid" in query:
                return [{"sid": "s_target", "mid": "m_target"}]
            if "RETURN a.id as src, b.id as dst" in query and "CALLS" in query:
                if "fa.project_id = $proj" in query and "fb.project_id = $proj" in query:
                    return [{"src": "m_caller", "dst": "m_target", "edge_type": "CALLS", "confidence": 0.9, "reason": "project"}]
                return [{"src": "external_caller", "dst": "m_target", "edge_type": "CALLS", "confidence": 0.9, "reason": "cross"}]
            if "DI_INJECT" in query or "INTERFACE_BINDING" in query:
                return []
            if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                ids = params.get("ids", []) if params else []
                out: list[dict] = []
                for mid in ids:
                    if mid == "m_target":
                        out.append({"id": "m_target", "name": "target", "fqname": "com.example.App#target", "file_path": "/app/Target.java", "project_id": "app", "class_fqcn": "com.example.App"})
                    elif mid == "m_caller":
                        out.append({"id": "m_caller", "name": "caller", "fqname": "com.example.Caller#caller", "file_path": "/app/Caller.java", "project_id": "app", "class_fqcn": "com.example.Caller"})
                    elif mid == "external_caller":
                        out.append({"id": "external_caller", "name": "external", "fqname": "com.other.Other#external", "file_path": "/other/External.java", "project_id": "other", "class_fqcn": "com.other.Other"})
                return out
            return []

    impact = analyze_impact(_ImpactScopeStore(), "target", project="app")

    assert impact["impacted_callers"]["1"][0]["name"] == "caller"
    assert impact["impacted_callers"]["1"][0]["project_id"] == "app"


def test_trace_execution_flows_scopes_traversal_by_project():
    class _FlowScopeStore:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "RETURN m.id as id, m.name as name, m.signature as fqname" in query:
                if "f.project_id = $proj" in query:
                    ids = params.get("ids", []) if params else []
                    mapping = {
                        "m_entry": {"id": "m_entry", "name": "main", "fqname": "com.example.App#main", "file_path": "/app/Main.java", "project_id": "app", "class_fqcn": "com.example.App"},
                        "m_helper": {"id": "m_helper", "name": "helper", "fqname": "com.example.App#helper", "file_path": "/app/Helper.java", "project_id": "app", "class_fqcn": "com.example.App"},
                        "external_method": {"id": "external_method", "name": "external", "fqname": "com.other.Other#external", "file_path": "/other/External.java", "project_id": "other", "class_fqcn": "com.other.Other"},
                    }
                    return [mapping[mid] for mid in ids if mid in mapping and mapping[mid]["project_id"] == "app"]
                ids = params.get("ids", []) if params else []
                mapping = {
                    "m_entry": {"id": "m_entry", "name": "main", "fqname": "com.example.App#main", "file_path": "/app/Main.java", "project_id": "app", "class_fqcn": "com.example.App"},
                    "m_helper": {"id": "m_helper", "name": "helper", "fqname": "com.example.App#helper", "file_path": "/app/Helper.java", "project_id": "app", "class_fqcn": "com.example.App"},
                    "external_method": {"id": "external_method", "name": "external", "fqname": "com.other.Other#external", "file_path": "/other/External.java", "project_id": "other", "class_fqcn": "com.other.Other"},
                }
                return [mapping[mid] for mid in ids if mid in mapping]
            if "RETURN m.id as id" in query and "f.project_id = $proj" in query:
                return [{"id": "m_entry"}]
            if "MATCH (a:Method)-[:CALLS]->(b:Method)" in query:
                if "fa.project_id = $proj" in query and "fb.project_id = $proj" in query:
                    return [{"src": "m_entry", "dst": "m_helper"}]
                return [{"src": "m_entry", "dst": "external_method"}]
            return []

    flows = trace_execution_flows(_FlowScopeStore(), entry_symbol="com.example.App#main", project="app")

    assert len(flows) == 1
    assert [node["name"] for node in flows[0]["nodes"]] == ["main", "helper"]
    assert all(node["project_id"] == "app" for node in flows[0]["nodes"])


def test_trace_execution_flows_metadata_resolution_honors_project(monkeypatch):
    class _Store:
        overlay_store = object()

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "RETURN m.id as id" in query and "f.project_id = $proj" in query:
                return [{"id": "m_entry"}]
            if "MATCH (a:Method)-[:CALLS]->(b:Method)" in query and "fa.project_id = $proj" in query:
                return [{"src": "m_entry", "dst": "m_hash"}]
            return []

    def fake_merged_method_records(store, overlay_store, project: str | None = None):
        if project == "app":
            return [
                {"id": "m_entry", "class_fqcn": "com.example.App", "signature": "main()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/Main.java", "name": "main"},
                {"id": "m_hash", "class_fqcn": "com.example.App", "signature": "helper()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/Helper.java", "name": "helper"},
            ]
        return [
            {"id": "m_entry", "class_fqcn": "com.example.App", "signature": "main()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/Main.java", "name": "main"},
            {"id": "m_hash", "class_fqcn": "com.other.Other", "signature": "helper()", "file_id": "f_other", "project_id": "other", "file_path": "/repo/other/Helper.java", "name": "helper"},
        ]

    monkeypatch.setattr("codespine.analysis.impact.merged_method_records", fake_merged_method_records)

    flows = trace_execution_flows(_Store(), entry_symbol="main", project="app")

    assert [node["project_id"] for node in flows[0]["nodes"]] == ["app", "app"]
    assert [node["file_path"] for node in flows[0]["nodes"]] == ["/repo/app/Main.java", "/repo/app/Helper.java"]


def test_resolve_entry_methods_prefers_qualified_exact_match_over_bare_name_fallback():
    class _Store:
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "lower(c.fqcn) = lower($class_fqcn)" in query:
                return [{"id": "m_target"}]
            if (params or {}).get("q") == "target" and ("lower(m.name) = lower($q)" in query or "lower(m.signature) = lower($q)" in query):
                return [{"id": "m_shadow"}]
            if "lower(m.name) = lower($q)" in query or "lower(m.signature) = lower($q)" in query:
                return []
            raise AssertionError(f"unexpected query: {query}")

    assert _resolve_entry_methods(_Store(), "com.example.App#target", project="app") == ["m_target"]


def test_analyze_impact_metadata_resolution_honors_project(monkeypatch):
    class _Store:
        overlay_store = object()

    def fake_merged_symbol_records(store, overlay_store, project: str | None = None):
        return [
            {"id": "s_target", "kind": "method", "name": "target", "fqname": "com.example.App#target()", "file_id": "f_app", "file_path": "/repo/app/App.java", "project_id": "app"},
            {"id": "s_caller", "kind": "method", "name": "caller", "fqname": "com.example.App#caller()", "file_id": "f_app", "file_path": "/repo/app/App.java", "project_id": "app"},
        ]

    def fake_merged_method_records(store, overlay_store, project: str | None = None):
        if project == "app":
            return [
                {"id": "m_target", "class_fqcn": "com.example.App", "signature": "target()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java", "name": "target"},
                {"id": "m_hash", "class_fqcn": "com.other.Other", "signature": "caller()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java", "name": "caller"},
            ]
        return [
            {"id": "m_target", "class_fqcn": "com.example.App", "signature": "target()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java", "name": "target"},
            {"id": "m_hash", "class_fqcn": "com.other.Other", "signature": "caller()", "file_id": "f_other", "project_id": "other", "file_path": "/repo/other/Other.java", "name": "caller"},
        ]

    def fake_merged_call_edges(store, overlay_store, project: str | None = None):
        return [{"src": "m_hash", "dst": "m_target", "confidence": 0.9, "reason": "project", "edge_type": "CALLS"}]

    monkeypatch.setattr("codespine.analysis.impact.merged_symbol_records", fake_merged_symbol_records)
    monkeypatch.setattr("codespine.analysis.impact.merged_method_records", fake_merged_method_records)
    monkeypatch.setattr("codespine.analysis.impact.merged_call_edges", fake_merged_call_edges)

    impact = analyze_impact(_Store(), "target", project="app")

    assert impact["impacted_callers"]["1"][0]["project_id"] == "app"
    assert impact["impacted_callers"]["1"][0]["file_path"] == "/repo/app/App.java"


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
                    {"symbol_id": "s1", "community_id": "comm1", "community_label": "Ordering"},
                    {"symbol_id": "s1", "community_id": "comm2", "community_label": "Payments"},
                ]
            if "IN_FLOW" in query:
                return [
                    {"symbol_id": "s1", "flow_id": "flow1", "flow_kind": "entry", "flow_depth": 0},
                    {"symbol_id": "s1", "flow_id": "flow2", "flow_kind": "exit", "flow_depth": 1},
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
    results = hybrid_search(_ExplainableStore(), "Fo", k=1, explain=True)

    assert results["retrieval_mode"] == "hybrid"
    assert results["query"] == "Fo"
    assert results["results"][0]["confidence"] == "medium"
    assert results["results"][0]["rank"] == 1
    assert results["results"][0]["confidence_reason"] == "Partial lexical match"
    assert "substring name match" in results["results"][0]["match_reasons"]
    assert results["results"][0]["retrieval_traces"]["bm25"]["rank"] == 1
    assert results["results"][0]["retrieval_traces"]["semantic"]["rank"] == 1
    assert results["retrieval_contract"]["fusion"] == "rrf"
    assert results["retrieval_contract"]["supports_rerank"] is True
    assert results["retrieval_contract"]["version"] == 12
    assert results["provenance"]["version"] == 12
    assert results["provenance"]["package_version"] == __version__
    assert results["provenance"]["index_fingerprint"]["snapshot_mtime"] >= 0.0
    assert "overlay_mtime" in results["provenance"]["index_fingerprint"]
    assert set(results["provenance"]["rankers"].keys()) == {"bm25", "semantic", "fuzzy"}


def test_hybrid_search_exact_match_fast_path_batches_context_and_skips_rankers(monkeypatch):
    store = _ExactMatchFastPathStore()

    monkeypatch.setattr("codespine.search.hybrid.rank_bm25", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bm25 should not run for exact matches")))
    monkeypatch.setattr("codespine.search.hybrid.rank_fuzzy", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fuzzy should not run for exact matches")))
    monkeypatch.setattr("codespine.search.hybrid.rank_semantic", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("semantic should not run for exact matches")))

    results = hybrid_search(store, "Foo", k=2, project="app")

    assert len(results) == 2
    assert sum(1 for q in store.queries if "IN_COMMUNITY" in q) == 1
    assert sum(1 for q in store.queries if "IN_FLOW" in q) == 1
    assert all(result["confidence"] == "high" for result in results)
    assert all(result["context"] for result in results)


def test_hybrid_search_exact_match_fast_path_orders_deterministically():
    results = hybrid_search(_ExactMatchOrderingStore(), "com.example.Foo", k=4, project="app")

    assert [result["id"] for result in results] == ["s-fqname", "com.example.Foo", "s-name-a", "s-name-z"]


def test_hybrid_search_exact_match_explain_preserves_contract(monkeypatch):
    store = _ExplainableStore()

    monkeypatch.setattr("codespine.search.hybrid.rank_bm25", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("bm25 should not run for exact matches")))
    monkeypatch.setattr("codespine.search.hybrid.rank_fuzzy", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fuzzy should not run for exact matches")))
    monkeypatch.setattr("codespine.search.hybrid.rank_semantic", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("semantic should not run for exact matches")))

    results = hybrid_search(store, "Foo", k=1, explain=True)

    assert results["retrieval_mode"] == "hybrid"
    assert results["results"][0]["confidence"] == "high"
    assert results["results"][0]["confidence_reason"] == "Exact name match"
    assert results["results"][0]["retrieval_traces"] == {}
    assert results["retrieval_contract"]["version"] == 12
    assert results["retrieval_contract"]["supports_rerank"] is True
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

    def fake_hybrid_search(store, query: str, k: int = 20, project: str | None = None, explain: bool = False, detail: str = "full", **kwargs):
        captured.update({"query": query, "k": k, "project": project, "explain": explain, "detail": detail})
        return {"retrieval_mode": "hybrid", "query": query, "results": [{"name": "Foo"}], "provenance": {"rankers": {}}}

    monkeypatch.setattr("codespine.cli.hybrid_search", fake_hybrid_search)

    result = CliRunner().invoke(main, ["search", "Foo", "--project", "app", "--explain", "--detail", "compact", "--json"])

    assert result.exit_code == 0
    assert captured == {"query": "Foo", "k": 20, "project": "app", "explain": True, "detail": "compact"}
    payload = json.loads(result.output)
    assert payload["retrieval_mode"] == "hybrid"


def test_mcp_search_hybrid_explain_flag_is_exposed_and_forwarded(monkeypatch):
    captured: dict[str, object] = {}

    class _NoopStore:
        def query_records(self, *args, **kwargs):
            return []

    def fake_hybrid_search(store, query: str, k: int = 20, project: str | None = None, explain: bool = False, detail: str = "full", **kwargs):
        captured.update({"query": query, "k": k, "project": project, "explain": explain, "detail": detail})
        return {"retrieval_mode": "hybrid", "results": [{"name": "Foo"}]}

    monkeypatch.setattr("codespine.mcp.server.hybrid_search", fake_hybrid_search)
    monkeypatch.setattr("codespine.mcp._tools_search.hybrid_search", fake_hybrid_search)

    async def _run():
        mcp = build_mcp_server(_NoopStore(), lambda: ".")
        tools = await mcp.list_tools()
        search_tool = next(tool for tool in tools if tool.name == "search_hybrid")
        assert "explain" in search_tool.parameters["properties"]
        assert "detail" in search_tool.parameters["properties"]
        assert search_tool.parameters["properties"]["explain"]["default"] is False
        assert search_tool.parameters["properties"]["detail"]["default"] == "full"
        await mcp.call_tool("search_hybrid", {"query": "Foo", "project": "app", "explain": True, "detail": "compact"})

    asyncio.run(_run())

    assert captured == {"query": "Foo", "k": 20, "project": "app", "explain": True, "detail": "compact"}
