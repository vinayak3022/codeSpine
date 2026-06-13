from __future__ import annotations

from typing import Any

from codespine.project_state import (
    derive_project_status,
    list_project_states,
    load_project_state,
    snapshot_info,
    synthetic_project_state,
)


def _count(store, query: str, params: dict[str, Any] | None = None) -> int:
    rows = store.query_records(query, params or {})
    if not rows:
        return 0
    total = 0
    for row in rows:
        if "n" in row:
            total += int(row["n"] or 0)
        elif "count" in row:
            total += int(row["count"] or 0)
        elif "total" in row:
            total += int(row["total"] or 0)
        elif "linked" in row:
            total += int(row["linked"] or 0)
        else:
            total += int(next(iter(row.values()), 0) or 0)
    return total


def graph_integrity_checks(store) -> dict[str, Any]:
    """Return graph-shape checks for dangling nodes and links."""
    checks = [
        (
            "files_without_project",
            "MATCH (f:File) RETURN count(f) as total",
            "MATCH (f:File), (p:Project) WHERE f.project_id = p.id RETURN count(f) as linked",
            "critical",
            "File nodes with missing project links",
        ),
        (
            "classes_without_file",
            "MATCH (c:Class) RETURN count(c) as total",
            "MATCH (c:Class), (f:File) WHERE c.file_id = f.id RETURN count(c) as linked",
            "critical",
            "Class nodes with missing file links",
        ),
        (
            "methods_without_class",
            "MATCH (m:Method) RETURN count(m) as total",
            "MATCH (m:Method), (c:Class) WHERE m.class_id = c.id RETURN count(m) as linked",
            "critical",
            "Method nodes with missing class links",
        ),
        (
            "symbols_without_file",
            "MATCH (s:Symbol) RETURN count(s) as total",
            "MATCH (s:Symbol), (f:File) WHERE s.file_id = f.id RETURN count(s) as linked",
            "warning",
            "Symbol nodes with missing file links",
        ),
    ]

    coverage: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    for code, total_query, linked_query, severity, label in checks:
        total = _count(store, total_query)
        linked = _count(store, linked_query)
        dangling = max(0, total - linked)
        coverage[code] = {"total": total, "linked": linked, "dangling": dangling}
        if dangling:
            issues.append(
                {
                    "severity": severity,
                    "code": code,
                    "count": dangling,
                    "message": f"{dangling} {label.lower()}.",
                }
            )

    return {"checks": coverage, "issues": issues, "issue_count": len(issues)}


def project_health(
    store,
    project_id: str,
    *,
    state: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    state = state or load_project_state(project_id)
    snapshot = snapshot or snapshot_info(project_id)
    project_state = derive_project_status(state, snapshot)
    if project_state == "partial":
        anomalies.append(
            {
                "severity": "warning",
                "code": "partial_core_index",
                "message": f"{project_id} has a partial core index. Run '{state.get('repair_hint') or f'codespine repair {project_id}'}'.",
            }
        )
    elif project_state == "degraded":
        anomalies.append(
            {
                "severity": "warning",
                "code": "deep_enrichment_failed",
                "message": state.get("last_error") or f"{project_id} needs deep-enrichment repair.",
            }
        )
    elif project_state == "repair_required":
        repair_message = state.get("last_error") or f"{project_id} has no valid published snapshot."
        anomalies.append(
            {
                "severity": "critical",
                "code": "repair_required",
                "message": repair_message,
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
        "project_state": project_state,
        "core_state": state.get("core_state"),
        "deep_state": state.get("deep_state"),
        "last_error": state.get("last_error"),
        "repair_hint": state.get("repair_hint"),
        "snapshot_valid": snapshot.get("snapshot_valid"),
        "write_db_valid": snapshot.get("write_db_valid"),
        "anomalies": anomalies,
    }


def index_health(store) -> dict[str, Any]:
    """Return project health plus aggregate anomaly counts."""
    state_by_id = {item.get("project_id"): item for item in list_project_states() if item.get("project_id")}
    try:
        projects = store.list_project_metadata()
    except AttributeError:
        projects = store.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path")
    meta_by_id = {item.get("id"): item for item in projects if item.get("id")}
    project_ids = sorted({pid for pid in list(meta_by_id) + list(state_by_id) if pid})
    per_project = []
    for pid in project_ids:
        project = meta_by_id.get(pid, {})
        state = state_by_id.get(pid) or synthetic_project_state(pid, path=project.get("path", ""))
        snap = snapshot_info(pid, store.router if hasattr(store, "router") else None)
        project_store = store.shard(pid) if hasattr(store, "shard") else store
        item = project_health(project_store, pid, state=state, snapshot=snap)
        item["path"] = state.get("path") or project.get("path")
        item["shard"] = snap.get("shard") if snap.get("shard") is not None else (store.router.shard_for(pid) if hasattr(store, "router") else None)
        per_project.append(item)
    anomalies = [a for p in per_project for a in p.get("anomalies", [])]
    graph_integrity = graph_integrity_checks(store)
    state_counts: dict[str, int] = {}
    for project in per_project:
        state = str(project.get("project_state") or "unknown")
        state_counts[state] = state_counts.get(state, 0) + 1
    return {
        "projects": per_project,
        "graph_integrity": graph_integrity,
        "summary": {
            "project_count": len(per_project),
            "anomaly_count": len(anomalies),
            "critical_count": sum(1 for a in anomalies if a.get("severity") == "critical"),
            "state_counts": state_counts,
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
        ("limit_param", "MATCH (m:Method) RETURN m.id as id LIMIT $lim", {"lim": 1}),
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
