from __future__ import annotations

import asyncio
import json
from pathlib import Path

from codespine.db.store import GraphStore
from codespine.health import index_health
from codespine.indexer.engine import JavaIndexer
from codespine.mcp.server import build_mcp_server
from codespine.analysis.deadcode import detect_dead_code
from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact
from codespine.search.hybrid import hybrid_search


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "java_simple"
GOLDEN_ROOT = Path(__file__).parent / "fixtures" / "goldens" / "java_simple"


class _DummyProc:
    pid = 0

    def poll(self):
        return 1


def _index_fixture(tmp_path: Path) -> tuple[GraphStore, str]:
    store = GraphStore(
        read_only=False,
        db_path_override=str(tmp_path / "db"),
        snapshot_path_override=str(tmp_path / "db_read"),
    )
    result = JavaIndexer(store).index_project(str(FIXTURE_ROOT), full=True)
    store.snapshot_to_read_replica(background=False)
    return store, result.project_id


def _rel(path: str | None) -> str | None:
    if not path:
        return path
    return str(Path(path).resolve().relative_to(FIXTURE_ROOT.resolve()))


def _guide_contract(payload: dict) -> dict:
    return {
        "section_ids": [section["id"] for section in payload["sections"]],
        "tool_names": {
            section["id"]: [tool["name"] for tool in section.get("tools", [])]
            for section in payload["sections"]
            if "tools" in section
        },
    }


def _capability_contract(payload: dict) -> dict:
    project = payload["indexed_projects"][0]
    features = payload["features"]
    health = payload["index_health"]
    return {
        "available": payload["available"],
        "project": {
            "project_id": project["project_id"],
            "path": Path(project["path"]).name,
            "project_state": project["project_state"],
            "core_state": project["core_state"],
            "deep_state": project["deep_state"],
        },
        "symbol_count": payload["symbol_count"],
        "features": {
            name: features[name]
            for name in (
                "search_hybrid",
                "get_impact",
                "get_symbol_context",
                "detect_dead_code",
                "trace_execution_flows",
                "get_neighborhood",
                "community_detection",
                "execution_flows",
                "change_coupling",
            )
        },
        "index_health": {
            "summary": health["summary"],
            "project_count": len(health["projects"]),
            "graph_integrity_issue_count": health["graph_integrity"]["issue_count"],
        },
    }


def _workflow_contract(store: GraphStore, project_id: str) -> dict:
    search = hybrid_search(store, "process payment", k=3, project=project_id)
    impact = analyze_impact(store, "processPayment", project=project_id)
    flows = trace_execution_flows(store, "main", project=project_id)
    deadcode = detect_dead_code(store, project=project_id)
    health = index_health(store)
    return {
        "search": {
            "result_count": len(search),
            "top_result": {
                "kind": search[0]["kind"],
                "name": search[0]["name"],
                "fqname": search[0].get("fqname"),
                "confidence": search[0].get("confidence"),
                "file_path": _rel(search[0].get("file_path")),
            },
        },
        "impact": {
            "resolved_to": [item["name"] for item in impact["resolved_to"]],
            "direct_callers": [item["name"] for item in impact["impacted_callers"]["1"]],
            "summary": impact["summary"],
        },
        "flows": {
            "count": len(flows),
            "paths": [[node["name"] for node in flow["nodes"]] for flow in flows],
        },
        "deadcode": {
            "dead_methods": [item["name"] for item in deadcode if isinstance(item, dict) and "method_id" in item],
            "stats": next(item["_stats"] for item in deadcode if isinstance(item, dict) and "_stats" in item),
        },
        "health": {
            "summary": health["summary"],
            "project": {
                "project_id": health["projects"][0]["project_id"],
                "project_state": health["projects"][0]["project_state"],
                "files": health["projects"][0]["files"],
                "classes": health["projects"][0]["classes"],
                "methods": health["projects"][0]["methods"],
                "calls": health["projects"][0]["calls"],
                "call_edge_coverage": health["projects"][0]["call_edge_coverage"],
            },
            "graph_integrity": {
                "issue_count": health["graph_integrity"]["issue_count"],
                "issue_codes": [item["code"] for item in health["graph_integrity"]["issues"]],
            },
        },
    }


def test_capability_contract_matches_golden(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("codespine.mcp.server.subprocess.Popen", lambda *args, **kwargs: _DummyProc())
    store, _project_id = _index_fixture(tmp_path)

    async def _run():
        mcp = build_mcp_server(store, lambda: str(FIXTURE_ROOT))
        guide_result = await mcp.call_tool("guide", {})
        capabilities_result = await mcp.call_tool("get_capabilities", {})
        return {
            "guide": _guide_contract(json.loads(guide_result.content[0].text)),
            "capabilities": _capability_contract(json.loads(capabilities_result.content[0].text)),
        }

    payload = asyncio.run(_run())
    assert payload == json.loads((GOLDEN_ROOT / "capabilities.json").read_text())


def test_fixture_backed_workflow_matches_golden(tmp_path: Path):
    store, project_id = _index_fixture(tmp_path)
    payload = _workflow_contract(store, project_id)
    assert payload == json.loads((GOLDEN_ROOT / "workflow.json").read_text())


class _FakeShard:
    def __init__(self, counts: dict[str, int]):
        self._counts = counts

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "count(f) as n" in query:
            return [{"n": self._counts.get("files", 0)}]
        if "count(c) as n" in query:
            return [{"n": self._counts.get("classes", 0)}]
        if "count(m) as n" in query and "DISTINCT" not in query:
            return [{"n": self._counts.get("methods", 0)}]
        if "count(*) as n" in query:
            return [{"n": self._counts.get("calls", 0)}]
        if "count(DISTINCT m.id) as n" in query:
            return [{"n": self._counts.get("methods_with_outgoing", 0)}]
        return [{"n": 0}]


class _FakeCapabilitiesStore:
    router = type("Router", (), {"shard_for": staticmethod(lambda project_id: 0 if project_id == "app-a" else 1)})()

    def __init__(self):
        self._shards = {
            "app-a": _FakeShard({"files": 10, "classes": 20, "methods": 90, "calls": 0, "methods_with_outgoing": 0}),
            "app-b": _FakeShard({"files": 5, "classes": 10, "methods": 40, "calls": 0, "methods_with_outgoing": 0}),
        }

    def list_project_metadata(self) -> list[dict]:
        return [
            {"id": "app-a", "path": str(FIXTURE_ROOT / "app-a")},
            {"id": "app-b", "path": str(FIXTURE_ROOT / "app-b")},
        ]

    def shard(self, project_id: str) -> _FakeShard:
        return self._shards[project_id]

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "MATCH (s:Symbol) RETURN count(s) as count" in query:
            return [{"count": 3}, {"count": 7}]
        if "MATCH (c:Community) RETURN count(c) as count" in query:
            return [{"count": 1}, {"count": 2}]
        if "MATCH (f:Flow) RETURN count(f) as count" in query:
            return [{"count": 4}, {"count": 5}]
        if "MATCH ()-[r:CO_CHANGED_WITH]->() RETURN count(r) as count" in query:
            return [{"count": 6}, {"count": 8}]
        if "MATCH (s:Symbol) WHERE s.embedding IS NOT NULL RETURN count(s) as count" in query:
            return [{"count": 2}, {"count": 3}]
        if "MATCH (f:File) RETURN count(f) as total" in query:
            return [{"total": 1}, {"total": 2}]
        if "MATCH (f:File), (p:Project)" in query:
            return [{"linked": 1}, {"linked": 1}]
        if "MATCH (c:Class) RETURN count(c) as total" in query:
            return [{"total": 2}, {"total": 3}]
        if "MATCH (c:Class), (f:File)" in query:
            return [{"linked": 1}, {"linked": 2}]
        if "MATCH (m:Method) RETURN count(m) as total" in query:
            return [{"total": 4}, {"total": 5}]
        if "MATCH (m:Method), (c:Class)" in query:
            return [{"linked": 2}, {"linked": 3}]
        if "MATCH (s:Symbol) RETURN count(s) as total" in query:
            return [{"total": 2}, {"total": 4}]
        if "MATCH (s:Symbol), (f:File)" in query:
            return [{"linked": 1}, {"linked": 2}]
        return [{"n": 0}]


def test_get_capabilities_aggregates_across_shards(monkeypatch):
    store = _FakeCapabilitiesStore()
    monkeypatch.setattr("codespine.mcp.server._project_inventory", lambda _store: [
        {
            "project_id": "app-a",
            "path": str(FIXTURE_ROOT / "app-a"),
            "project_state": "ready",
            "core_state": "ready",
            "deep_state": "idle",
            "last_error": "",
            "repair_hint": "",
            "snapshot_valid": True,
            "write_db_valid": True,
        },
        {
            "project_id": "app-b",
            "path": str(FIXTURE_ROOT / "app-b"),
            "project_state": "ready",
            "core_state": "ready",
            "deep_state": "idle",
            "last_error": "",
            "repair_hint": "",
            "snapshot_valid": True,
            "write_db_valid": True,
        },
    ])
    monkeypatch.setattr("codespine.mcp.server._git_available", lambda _path: True)
    monkeypatch.setattr("codespine.search.vector._load_model", lambda: None)
    monkeypatch.setattr("codespine.mcp.server.subprocess.Popen", lambda *args, **kwargs: _DummyProc())

    async def _run():
        mcp = build_mcp_server(store, lambda: str(FIXTURE_ROOT))
        result = await mcp.call_tool("get_capabilities", {})
        return json.loads(result.content[0].text)

    payload = asyncio.run(_run())
    assert payload["symbol_count"] == 10
    assert payload["index_health"]["summary"] == {
        "project_count": 2,
        "anomaly_count": 0,
        "critical_count": 0,
        "state_counts": {"ready": 2},
    }
    assert payload["index_health"]["graph_integrity"]["checks"]["files_without_project"] == {
        "total": 3,
        "linked": 2,
        "dangling": 1,
    }
