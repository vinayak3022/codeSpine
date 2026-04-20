from __future__ import annotations

from typing import Any


def _count(store, query: str, params: dict[str, Any] | None = None) -> int:
    rows = store.query_records(query, params or {})
    if not rows:
        return 0
    row = rows[0]
    if "n" in row:
        return int(row["n"])
    if "count" in row:
        return int(row["count"])
    return int(next(iter(row.values()), 0))


def project_health(store, project_id: str) -> dict[str, Any]:
    """Return indexing health metrics for one project."""
    files = _count(store, "MATCH (f:File) WHERE f.project_id = $pid RETURN count(f) as n", {"pid": project_id})
    classes = _count(
        store,
        "MATCH (c:Class), (f:File) WHERE c.file_id = f.id AND f.project_id = $pid RETURN count(c) as n",
        {"pid": project_id},
    )
    methods = _count(
        store,
        "MATCH (m:Method), (c:Class), (f:File) "
        "WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid RETURN count(m) as n",
        {"pid": project_id},
    )
    calls = _count(
        store,
        "MATCH (ma:Method)-[:CALLS]->(mb:Method), (ca:Class), (fa:File) "
        "WHERE ma.class_id = ca.id AND ca.file_id = fa.id AND fa.project_id = $pid RETURN count(*) as n",
        {"pid": project_id},
    )
    methods_with_outgoing = _count(
        store,
        "MATCH (m:Method)-[:CALLS]->(), (c:Class), (f:File) "
        "WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid RETURN count(DISTINCT m.id) as n",
        {"pid": project_id},
    )
    coverage = (methods_with_outgoing / methods) if methods else 0.0
    anomalies: list[dict[str, Any]] = []
    if methods >= 100 and calls == 0:
        anomalies.append(
            {
                "severity": "critical",
                "code": "zero_call_edges",
                "message": f"{project_id} has {methods} methods but 0 call edges.",
            }
        )
    elif methods >= 100 and coverage < 0.01:
        anomalies.append(
            {
                "severity": "warning",
                "code": "low_call_coverage",
                "message": f"{project_id} call-edge coverage is {coverage:.1%}.",
            }
        )
    return {
        "project_id": project_id,
        "files": files,
        "classes": classes,
        "methods": methods,
        "calls": calls,
        "methods_with_outgoing_calls": methods_with_outgoing,
        "call_edge_coverage": round(coverage, 4),
        "anomalies": anomalies,
    }


def index_health(store) -> dict[str, Any]:
    """Return project health plus aggregate anomaly counts."""
    try:
        projects = store.list_project_metadata()
    except AttributeError:
        projects = store.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path")
    per_project = []
    for project in projects:
        pid = project.get("id")
        if not pid:
            continue
        project_store = store.shard(pid) if hasattr(store, "shard") else store
        item = project_health(project_store, pid)
        item["path"] = project.get("path")
        item["shard"] = store.router.shard_for(pid) if hasattr(store, "router") else None
        per_project.append(item)
    anomalies = [a for p in per_project for a in p.get("anomalies", [])]
    return {
        "projects": per_project,
        "summary": {
            "project_count": len(per_project),
            "anomaly_count": len(anomalies),
            "critical_count": sum(1 for a in anomalies if a.get("severity") == "critical"),
        },
    }


def smoke_test_index(store) -> dict[str, Any]:
    """Run a small query suite that catches schema/translator regressions."""
    checks = [
        ("projects", "MATCH (p:Project) RETURN count(p) as n", {}),
        ("symbols", "MATCH (s:Symbol) RETURN count(s) as n", {}),
        ("contains_lower_rhs", "MATCH (s:Symbol) WHERE lower(s.name) CONTAINS lower($q) RETURN count(s) as n", {"q": "x"}),
        ("method_project_scope", "MATCH (m:Method) WHERE m.project_id = $pid RETURN count(m) as n", {"pid": "__none__"}),
        ("edge_count", "MATCH ()-[r]->() RETURN count(r) as n", {}),
    ]
    results: list[dict[str, Any]] = []
    for name, query, params in checks:
        try:
            store.query_records(query, params)
            results.append({"name": name, "ok": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"name": name, "ok": False, "error": str(exc)[:500]})
    failed = [r for r in results if not r["ok"]]
    return {
        "ok": not failed,
        "checks": results,
        "failed_count": len(failed),
    }

