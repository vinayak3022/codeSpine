from __future__ import annotations

from collections import defaultdict, deque


def _resolve_symbol_ids(store, symbol_query: str, project: str | None = None) -> list[str]:
    project_clause = "AND f.project_id = $proj" if project else ""
    params: dict = {"q": symbol_query}
    if project:
        params["proj"] = project
    recs = store.query_records(
        f"""
        MATCH (s:Symbol), (f:File)
        WHERE s.file_id = f.id {project_clause}
        AND (s.id = $q OR lower(s.name) = lower($q) OR lower(s.fqname) = lower($q) OR lower(s.fqname) CONTAINS lower($q))
        RETURN s.id as id
        LIMIT 50
        """,
        params,
    )
    return [r["id"] for r in recs]


def analyze_impact(store, symbol_query: str, max_depth: int = 4, project: str | None = None) -> dict:
    target_symbol_ids = _resolve_symbol_ids(store, symbol_query, project=project)
    if not target_symbol_ids:
        return {"target": symbol_query, "depth_groups": {"1": [], "2": [], "3+": []}}

    symbol_to_method = {
        r["sid"]: r["mid"]
        for r in store.query_records(
            """
            MATCH (s:Symbol),(m:Method)
            WHERE s.kind = 'method' AND s.fqname CONTAINS m.signature
            RETURN s.id as sid, m.id as mid
            """
        )
    }

    target_method_ids = [symbol_to_method[sid] for sid in target_symbol_ids if sid in symbol_to_method]
    if not target_method_ids:
        return {"target": symbol_query, "depth_groups": {"1": [], "2": [], "3+": []}}

    # Load all call edges – cross-project callers are included intentionally so
    # impact analysis surfaces inter-module dependencies.
    edges = store.query_records(
        """
        MATCH (a:Method)-[r:CALLS]->(b:Method)
        RETURN a.id as src, b.id as dst, 'CALLS' as edge_type,
               coalesce(r.confidence, 0.5) as confidence,
               coalesce(r.reason, 'unknown') as reason
        """
    )

    reverse_adj: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        reverse_adj[edge["dst"]].append(edge)

    depth_groups: dict[str, list[dict]] = {"1": [], "2": [], "3+": []}
    visited: set[str] = set(target_method_ids)
    queue = deque([(mid, 0, [mid]) for mid in target_method_ids])

    while queue:
        node, depth, path = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in reverse_adj.get(node, []):
            src = edge["src"]
            if src in visited:
                continue
            visited.add(src)
            next_depth = depth + 1
            item = {
                "symbol": src,
                "depth": next_depth,
                "edge_type": edge["edge_type"],
                "confidence": float(edge["confidence"]),
                "path": path + [src],
            }
            if next_depth == 1:
                depth_groups["1"].append(item)
            elif next_depth == 2:
                depth_groups["2"].append(item)
            else:
                depth_groups["3+"].append(item)
            queue.append((src, next_depth, path + [src]))

    return {
        "target": symbol_query,
        "targets_resolved": target_method_ids,
        "depth_groups": depth_groups,
        "summary": {
            "direct": len(depth_groups["1"]),
            "indirect": len(depth_groups["2"]),
            "transitive": len(depth_groups["3+"]),
        },
    }
