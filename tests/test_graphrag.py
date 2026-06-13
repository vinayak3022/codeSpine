from __future__ import annotations

import asyncio
import json
import os

from click.testing import CliRunner

from codespine.cli import main
from codespine.graphrag import _GRAPH_RAG_CACHE, graph_rag_answer
from codespine.mcp.server import build_mcp_server


class _NoopStore:
    def query_records(self, *args, **kwargs):
        return []


def test_graph_rag_answer_builds_contracts(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
            "confidence_reason": "Exact name match",
            "snippet": "public void processPayment() {}",
        },
        "search_candidates": [
            {
                "id": "m1",
                "name": "processPayment",
                "fqname": "com.example.PaymentService#processPayment",
                "file_path": "/tmp/PaymentService.java",
                "line": 12,
                "score": 0.97,
                "confidence": "high",
                "snippet": "public void processPayment() {}",
            }
        ],
        "impact": {
            "impacted_callers": {
                "1": [
                    {
                        "symbol": "m2",
                        "name": "checkout",
                        "fqname": "com.example.OrderController#checkout",
                        "file_path": "/tmp/OrderController.java",
                        "depth": 1,
                        "edge_type": "CALLS",
                        "confidence": 0.9,
                        "path": ["checkout", "processPayment"],
                    }
                ],
                "2": [],
                "3+": [],
            },
            "summary": {"direct": 1, "indirect": 0, "transitive": 0, "self_callers": 0},
        },
        "community": {"community_id": "c1", "community_label": "Payments", "cohesion": 0.87},
        "flows": [{"flow_id": "f1", "flow_kind": "entry", "flow_depth": 0}],
    }
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: context)

    result = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")

    assert result["available"] is True
    assert result["confidence"]["label"] == "high"
    assert result["evidence"]
    assert result["citations"]
    assert result["evidence_subgraph"]["nodes"]
    assert result["evidence_subgraph"]["edges"]
    assert result["evidence"][0]["citation_id"] == result["citations"][0]["id"]
    assert result["evidence"][0]["subgraph"]["nodes"]
    assert any(item["kind"] == "community" for item in result["evidence"])
    assert any(item["kind"] == "flow" for item in result["evidence"])
    community_citation = next(item for item in result["citations"] if item["kind"] == "community")
    flow_citation = next(item for item in result["citations"] if item["kind"] == "flow")
    assert community_citation["source"] == "symbol_community"
    assert community_citation["community_id"] == "c1"
    assert flow_citation["source"] == "trace_execution_flows"
    assert flow_citation["flow_id"] == "f1"
    assert result["observability"]["retrieval_mode"] == "graph_rag"
    assert result["observability"]["k"] == 5
    assert result["observability"]["evidence_rerank"]["strategy"] == "utility_ranked"
    assert result["observability"]["evidence_rerank"]["selected"][0]["title"] == "com.example.PaymentService#processPayment"
    assert result["observability"]["evidence_rerank"]["selected"][0]["citation_id"] == "c1"
    assert result["observability"]["evidence_rerank"]["selected"][0]["score"] is not None
    assert result["supporting_context"]["impact_summary"]["direct"] == 1
    assert result["supporting_context"]["evidence_subgraph_nodes"] >= 1
    assert result["confidence"]["evidence_count"] == 4
    assert "analyze_impact" in result["confidence"]["supporting_signals"]


def test_graph_rag_answer_caches_by_snapshot_and_reports_latency(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
        },
        "search_candidates": [
            {"id": "m1", "name": "processPayment", "fqname": "com.example.PaymentService#processPayment", "file_path": "/tmp/PaymentService.java", "line": 12, "score": 0.97, "confidence": "high"},
        ],
        "impact": {"impacted_callers": {"1": [{"symbol": "m2", "name": "checkout", "fqname": "com.example.OrderController#checkout", "file_path": "/tmp/OrderController.java", "depth": 1, "edge_type": "CALLS", "confidence": 0.9}], "2": [], "3+": []}, "summary": {"direct": 1, "indirect": 0, "transitive": 0, "self_callers": 0}},
        "community": {"community_id": "c1", "community_label": "Payments", "cohesion": 0.87},
        "flows": [{"flow_id": "f1", "flow_kind": "entry", "flow_depth": 0}],
        "timings_ms": {"search": 1, "impact": 2, "community": 3, "flows": 4, "total": 10},
    }
    calls = {"count": 0}

    _GRAPH_RAG_CACHE.invalidate()
    monkeypatch.setattr("codespine.graphrag._store_snapshot_mtime", lambda *args, **kwargs: 7.0)
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: calls.__setitem__("count", calls["count"] + 1) or context)

    first = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")
    second = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")

    assert calls["count"] == 1
    assert first["observability"]["cache"]["hit"] is False
    assert first["observability"]["latency_ms"]["cache_lookup"] == 0
    assert second["observability"]["cache"]["hit"] is True
    assert second["observability"]["latency_ms"]["context"]["search"] == 1
    assert second["observability"]["latency_ms"]["cache_lookup"] >= 0


def test_graph_rag_answer_cache_invalidates_on_overlay_change(monkeypatch, tmp_path):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
        },
        "search_candidates": [{"id": "m1", "name": "processPayment", "fqname": "com.example.PaymentService#processPayment", "file_path": "/tmp/PaymentService.java", "line": 12, "score": 0.97, "confidence": "high"}],
        "impact": {"impacted_callers": {"1": [], "2": [], "3+": []}, "summary": {}},
        "community": None,
        "flows": [],
        "timings_ms": {"search": 1, "impact": 0, "community": 0, "flows": 0, "total": 1},
    }
    calls = {"count": 0}
    overlay_path = tmp_path / "overlay" / "app.json"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text("{}", encoding="utf-8")

    class _OverlayStore:
        def project_path(self, project: str):
            return str(overlay_path)

    class _Store:
        overlay_store = _OverlayStore()

    _GRAPH_RAG_CACHE.invalidate()
    monkeypatch.setattr("codespine.graphrag._store_snapshot_mtime", lambda *args, **kwargs: 7.0)
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: calls.__setitem__("count", calls["count"] + 1) or context)

    graph_rag_answer(_Store(), "what breaks if I change Foo?", project="app")
    os.utime(overlay_path, (11.1, 11.1))
    graph_rag_answer(_Store(), "what breaks if I change Foo?", project="app")

    assert calls["count"] == 2


def test_graph_rag_answer_cache_invalidates_on_rapid_snapshot_updates(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
        },
        "search_candidates": [{"id": "m1", "name": "processPayment", "fqname": "com.example.PaymentService#processPayment", "file_path": "/tmp/PaymentService.java", "line": 12, "score": 0.97, "confidence": "high"}],
        "impact": {"impacted_callers": {"1": [], "2": [], "3+": []}, "summary": {}},
        "community": None,
        "flows": [],
        "timings_ms": {"search": 1, "impact": 0, "community": 0, "flows": 0, "total": 1},
    }
    calls = {"count": 0}
    snapshot_mtimes = iter([11.1, 11.2])

    _GRAPH_RAG_CACHE.invalidate()
    monkeypatch.setattr("codespine.graphrag._store_snapshot_mtime", lambda *args, **kwargs: next(snapshot_mtimes))
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: calls.__setitem__("count", calls["count"] + 1) or context)

    graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")
    graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")

    assert calls["count"] == 2


def test_graph_rag_answer_cache_invalidates_on_snapshot_change(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
        },
        "search_candidates": [{"id": "m1", "name": "processPayment", "fqname": "com.example.PaymentService#processPayment", "file_path": "/tmp/PaymentService.java", "line": 12, "score": 0.97, "confidence": "high"}],
        "impact": {"impacted_callers": {"1": [], "2": [], "3+": []}, "summary": {}},
        "community": None,
        "flows": [],
        "timings_ms": {"search": 1, "impact": 0, "community": 0, "flows": 0, "total": 1},
    }
    calls = {"count": 0}
    snapshot_mtimes = iter([11.0, 12.0])

    _GRAPH_RAG_CACHE.invalidate()
    monkeypatch.setattr("codespine.graphrag._store_snapshot_mtime", lambda *args, **kwargs: next(snapshot_mtimes))
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: calls.__setitem__("count", calls["count"] + 1) or context)

    graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")
    graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")

    assert calls["count"] == 2


def test_graph_rag_answer_normalizes_real_flow_shape_and_keeps_citations_unique(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
            "snippet": "public void processPayment() {}",
        },
        "search_candidates": [],
        "impact": {"impacted_callers": {"1": [], "2": [], "3+": []}, "summary": {"direct": 0, "indirect": 0, "transitive": 0, "self_callers": 0}},
        "community": None,
        "flows": [
            {"entry": "m1", "kind": "intra_community", "nodes": [{"symbol": "m1", "depth": 0}, {"symbol": "m2", "depth": 1}]},
            {"entry": "m3", "kind": "intra_community", "nodes": [{"symbol": "m3", "depth": 0}, {"symbol": "m4", "depth": 1}]},
        ],
    }
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: context)

    result = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")

    flow_citations = [item for item in result["citations"] if item["kind"] == "flow"]
    assert [item["citation_id"] for item in result["evidence"] if item["kind"] == "flow"] == [c["id"] for c in flow_citations]
    assert len(flow_citations) == 2
    assert len({item["id"] for item in flow_citations}) == 2
    assert {item["flow_id"] for item in flow_citations} == {"m1", "m3"}
    assert result["observability"]["primitives"][-1] == "trace_execution_flows"


def test_graph_rag_answer_honors_k_as_evidence_budget(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
            "snippet": "public void processPayment() {}",
        },
        "search_candidates": [
            {"id": "m1", "name": "processPayment", "fqname": "com.example.PaymentService#processPayment", "file_path": "/tmp/PaymentService.java", "line": 12, "score": 0.97, "confidence": "high"},
            {"id": "m3", "name": "refund", "fqname": "com.example.PaymentService#refund", "file_path": "/tmp/PaymentService.java", "line": 20, "score": 0.72, "confidence": "medium"},
            {"id": "m4", "name": "capture", "fqname": "com.example.PaymentService#capture", "file_path": "/tmp/PaymentService.java", "line": 30, "score": 0.66, "confidence": "medium"},
        ],
        "impact": {
            "impacted_callers": {
                "1": [{"symbol": "m2", "name": "checkout", "fqname": "com.example.OrderController#checkout", "file_path": "/tmp/OrderController.java", "depth": 1, "edge_type": "CALLS", "confidence": 0.9}],
                "2": [{"symbol": "m5", "name": "receipt", "fqname": "com.example.ReceiptService#receipt", "file_path": "/tmp/ReceiptService.java", "depth": 2, "edge_type": "CALLS", "confidence": 0.8}],
                "3+": [{"symbol": "m6", "name": "audit", "fqname": "com.example.AuditService#audit", "file_path": "/tmp/AuditService.java", "depth": 3, "edge_type": "CALLS", "confidence": 0.7}],
            },
            "summary": {"direct": 1, "indirect": 1, "transitive": 1, "self_callers": 0},
        },
        "community": {"community_id": "c1", "community_label": "Payments", "cohesion": 0.87},
        "flows": [{"flow_id": "f1", "flow_kind": "entry", "flow_depth": 0}],
    }
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: context)

    result = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app", k=2)

    assert len(result["evidence"]) == 2
    assert len(result["citations"]) == 2
    assert [item["kind"] for item in result["evidence"]] == ["search_result", "impact"]
    assert result["observability"]["k"] == 2
    assert result["observability"]["evidence_count"] == 2
    assert result["observability"]["citation_count"] == 2
    assert result["observability"]["evidence_rerank"]["candidate_counts"]["impact"] == 3
    assert result["supporting_context"]["evidence_kinds"] == ["search_result", "impact"]


def test_graph_rag_answer_confidence_fallback_reflects_selected_evidence(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
            "snippet": "public void processPayment() {}",
        },
        "search_candidates": [
            {"id": "m1", "name": "processPayment", "fqname": "com.example.PaymentService#processPayment", "file_path": "/tmp/PaymentService.java", "line": 12, "score": 0.97, "confidence": "high"},
        ],
        "impact": {
            "impacted_callers": {
                "1": [{"symbol": "m2", "name": "checkout", "fqname": "com.example.OrderController#checkout", "file_path": "/tmp/OrderController.java", "depth": 1, "edge_type": "CALLS", "confidence": 0.9}],
                "2": [],
                "3+": [],
            },
            "summary": {"direct": 1, "indirect": 0, "transitive": 0, "self_callers": 0},
        },
        "community": {"community_id": "c1", "community_label": "Payments", "cohesion": 0.87},
        "flows": [{"flow_id": "f1", "flow_kind": "entry", "flow_depth": 0}],
    }
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: context)

    result = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app", k=4)

    reason = result["confidence"]["reason"]
    assert "search" in reason and "impact" in reason and "community" in reason and "flow" in reason
    assert "hybrid_search" in reason and "analyze_impact" in reason and "symbol_community" in reason and "trace_execution_flows" in reason
    assert "search and impact analysis" not in reason


def test_graph_rag_answer_preserves_focus_anchor_when_focus_is_evidence(monkeypatch):
    context = {
        "query": "what breaks if I change Foo?",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "processPayment",
            "fqname": "com.example.PaymentService#processPayment",
            "file_path": "/tmp/PaymentService.java",
            "line": 12,
            "score": 0.97,
            "confidence": "high",
            "snippet": "public void processPayment() {}",
        },
        "search_candidates": [
            {
                "id": "m1",
                "name": "processPayment",
                "fqname": "com.example.PaymentService#processPayment",
                "file_path": "/tmp/PaymentService.java",
                "line": 12,
                "score": 0.97,
                "confidence": "high",
                "snippet": "public void processPayment() {}",
            }
        ],
        "impact": {"impacted_callers": {"1": [], "2": [], "3+": []}, "summary": {"direct": 0, "indirect": 0, "transitive": 0, "self_callers": 0}},
        "community": None,
        "flows": [],
    }
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: context)

    result = graph_rag_answer(_NoopStore(), "what breaks if I change Foo?", project="app")

    focus_nodes = [node for node in result["evidence_subgraph"]["nodes"] if node.get("role") == "focus"]
    assert len(focus_nodes) == 1
    assert focus_nodes[0]["id"] == "m1"
    assert any(node.get("symbol_id") == "m1" and node.get("role") != "focus" for node in result["evidence_subgraph"]["nodes"])
    assert all(edge["source"] != edge["target"] for edge in result["evidence_subgraph"]["edges"])


def test_graph_rag_answer_returns_unavailable_when_no_focus(monkeypatch):
    monkeypatch.setattr(
        "codespine.graphrag.build_symbol_context",
        lambda *args, **kwargs: {"query": "unknown", "focus": None, "search_candidates": [], "impact": {}, "community": None, "flows": []},
    )

    result = graph_rag_answer(_NoopStore(), "unknown")

    assert result["available"] is False
    assert result["abstained"] is True
    assert "GraphRAG answer" in result["note"]
    assert result["fallback"]["recommended_tools"] == ["find_symbol", "search_hybrid"]
    assert result["answer_contract"]["status"] == "abstained"
    assert result["answer"] == ""
    assert result["focus"] == {}
    assert result["evidence"] == []
    assert result["citations"] == []
    assert result["evidence_subgraph"] == {"nodes": [], "edges": []}
    assert result["supporting_context"]["search_candidate_count"] == 0
    assert result["supporting_context"]["evidence_subgraph_nodes"] == 0
    assert result["observability"]["evidence_count"] == 0
    assert result["observability"]["citation_count"] == 0


def test_graph_rag_answer_abstains_on_near_competing_symbol_candidates(monkeypatch):
    context = {
        "query": "loadAccount",
        "focus": {
            "id": "m1",
            "kind": "method",
            "name": "loadAccount",
            "fqname": "com.example.alpha.AccountService#loadAccount",
            "file_path": "/tmp/alpha/AccountService.java",
            "line": 12,
            "score": 0.98,
            "confidence": "high",
        },
        "search_candidates": [
            {"id": "m1", "name": "loadAccount", "fqname": "com.example.alpha.AccountService#loadAccount", "file_path": "/tmp/alpha/AccountService.java", "line": 12, "score": 0.98, "confidence": "high"},
            {"id": "m2", "name": "loadAccounts", "fqname": "com.example.beta.AccountReader#loadAccounts", "file_path": "/tmp/beta/AccountReader.java", "line": 18, "score": 0.965, "confidence": "high"},
        ],
        "impact": {"impacted_callers": {"1": [], "2": [], "3+": []}, "summary": {}},
        "community": None,
        "flows": [],
    }
    monkeypatch.setattr("codespine.graphrag.build_symbol_context", lambda *args, **kwargs: context)

    result = graph_rag_answer(_NoopStore(), "loadAccount")

    assert result["available"] is False
    assert result["abstained"] is True
    assert result["ambiguity"]["status"] == "ambiguous"
    assert result["ambiguity"]["alternatives"][0]["id"] == "m2"
    assert "ambiguous" in result["note"].lower()
    assert result["answer"] == ""
    assert result["focus"] == {}
    assert result["evidence"] == []
    assert result["citations"] == []
    assert result["supporting_context"]["search_candidate_count"] == 2
    assert result["answer_contract"]["status"] == "abstained"


def test_graph_rag_answer_abstains_on_weak_grounding_without_ambiguity(monkeypatch):
    monkeypatch.setattr(
        "codespine.graphrag.build_symbol_context",
        lambda *args, **kwargs: {"query": "unknown", "focus": None, "search_candidates": [], "impact": {}, "community": None, "flows": []},
    )

    result = graph_rag_answer(_NoopStore(), "unknown")

    assert result["available"] is False
    assert result["abstained"] is True
    assert "ambiguous" not in result
    assert result.get("ambiguity") is None
    assert result["answer_contract"]["status"] == "abstained"
    assert result["answer_contract"]["ambiguity"] is None
    assert result["answer"] == ""
    assert result["focus"] == {}
    assert result["evidence"] == []
    assert result["citations"] == []
    assert result["supporting_context"]["search_candidate_count"] == 0


def test_cli_answer_forwards_question_and_contract(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr("codespine.cli._open_store", lambda read_only=True: object())

    def fake_graph_rag_answer(store, question: str, *, project: str | None = None, max_depth: int = 3, k: int = 5):
        captured.update({"question": question, "project": project, "max_depth": max_depth, "k": k})
        return {"available": True, "answer": "ok", "confidence": {"label": "high", "score": 0.9, "reason": "x"}, "evidence": [], "citations": [], "observability": {"retrieval_mode": "graph_rag"}}

    monkeypatch.setattr("codespine.cli.graph_rag_answer", fake_graph_rag_answer)

    result = CliRunner().invoke(main, ["answer", "what breaks if I change Foo?", "--project", "app", "--json"])

    assert result.exit_code == 0
    assert captured == {"question": "what breaks if I change Foo?", "project": "app", "max_depth": 3, "k": 5}
    payload = json.loads(result.output)
    assert payload["answer"] == "ok"


def test_mcp_answer_tool_is_exposed_and_forwarded(monkeypatch):
    captured: dict[str, object] = {}

    def fake_graph_rag_answer(store, question: str, *, project: str | None = None, max_depth: int = 3, k: int = 5):
        captured.update({"question": question, "project": project, "max_depth": max_depth, "k": k})
        return {"available": True, "answer": "ok", "confidence": {"label": "high", "score": 0.9, "reason": "x"}, "evidence": [], "citations": [], "observability": {"retrieval_mode": "graph_rag"}}

    monkeypatch.setattr("codespine.mcp.server.graph_rag_answer", fake_graph_rag_answer)

    async def _run():
        mcp = build_mcp_server(_NoopStore(), lambda: ".")
        tools = await mcp.list_tools()
        answer_tool = next(tool for tool in tools if tool.name == "answer")
        assert "question" in answer_tool.parameters["properties"]
        await mcp.call_tool("answer", {"question": "what breaks if I change Foo?", "project": "app"})

    asyncio.run(_run())

    assert captured == {"question": "what breaks if I change Foo?", "project": "app", "max_depth": 3, "k": 5}


def test_mcp_answer_tool_preserves_unavailable_result_shape(monkeypatch):
    monkeypatch.setattr(
        "codespine.mcp.server.graph_rag_answer",
        lambda *args, **kwargs: {
            "available": False,
            "abstained": True,
            "question": "unknown",
            "answer": "",
            "focus": {},
            "confidence": {"label": "low", "score": 0.0, "reason": "No symbol match found for a GraphRAG answer.", "evidence_count": 0, "evidence_kinds": [], "supporting_signals": []},
            "evidence": [],
            "citations": [],
            "evidence_subgraph": {"nodes": [], "edges": []},
            "note": "No symbol match found for a GraphRAG answer.",
            "fallback": {"recommended_tools": ["find_symbol", "search_hybrid"], "reason": "The answer surface refused to guess without a unique, grounded symbol match."},
            "answer_contract": {"status": "abstained", "grounded": False, "requires_citations": True, "fallback_mode": True, "ambiguity": None, "supported_by": []},
            "supporting_context": {"impact_summary": {}, "community": None, "flow_count": 0, "search_candidate_count": 0, "community_label": None, "evidence_kinds": [], "evidence_sources": [], "evidence_subgraph_nodes": 0, "evidence_subgraph_edges": 0, "context_note": None},
            "observability": {"retrieval_mode": "graph_rag"},
        },
    )

    async def _run():
        mcp = build_mcp_server(_NoopStore(), lambda: ".")
        result = await mcp.call_tool("answer", {"question": "unknown", "project": "app"})
        payload = json.loads(result.content[0].text)
        assert payload["available"] is False
        assert payload["abstained"] is True
        assert payload["answer"] == ""
        assert payload["focus"] == {}
        assert payload["evidence"] == []
        assert payload["citations"] == []
        assert payload["evidence_subgraph"] == {"nodes": [], "edges": []}

    asyncio.run(_run())


def test_mcp_answer_tool_preserves_empty_evidence_shape(monkeypatch):
    monkeypatch.setattr(
        "codespine.mcp.server.graph_rag_answer",
        lambda *args, **kwargs: {
            "available": True,
            "answer": "ok",
            "confidence": {"label": "high", "score": 0.9, "reason": "x"},
            "evidence": [],
            "citations": [],
            "observability": {"retrieval_mode": "graph_rag"},
        },
    )

    async def _run():
        mcp = build_mcp_server(_NoopStore(), lambda: ".")
        result = await mcp.call_tool("answer", {"question": "what breaks if I change Foo?", "project": "app"})
        payload = json.loads(result.content[0].text)
        assert payload["evidence"] == []
        assert payload["citations"] == []

    asyncio.run(_run())
