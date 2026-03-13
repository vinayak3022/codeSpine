from __future__ import annotations

from collections import defaultdict, deque

from codespine.overlay.merge import merged_call_edges, merged_method_records, merged_symbol_records


def _resolve_symbol_ids(store, symbol_query: str, project: str | None = None) -> list[str]:
    overlay_store = getattr(store, "overlay_store", None)
    if overlay_store is not None:
        recs = []
        needle = symbol_query.lower()
        for rec in merged_symbol_records(store, overlay_store, project=project):
            name = str(rec.get("name") or "").lower()
            fqname = str(rec.get("fqname") or "").lower()
            if rec.get("id") == symbol_query or name == needle or fqname == needle or needle in fqname:
                recs.append({"id": rec["id"]})
                if len(recs) >= 50:
                    break
    else:
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


def _resolve_method_metadata(store, method_ids: list[str]) -> dict[str, dict]:
    """Bulk-resolve method IDs to human-readable metadata in a single query.

    Returns a dict keyed by method ID with fields:
      name, fqname (= m.signature), class_fqcn, file_path, project_id.
    Any ID not found in the graph is silently omitted.
    """
    if not method_ids:
        return {}
    overlay_store = getattr(store, "overlay_store", None)
    if overlay_store is not None:
        recs = [r for r in merged_method_records(store, overlay_store) if r.get("id") in set(method_ids)]
        for rec in recs:
            rec["fqname"] = rec.get("signature")
    else:
        recs = store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.id IN $ids AND m.class_id = c.id AND c.file_id = f.id
            RETURN m.id as id, m.name as name, m.signature as fqname,
                   c.fqcn as class_fqcn, f.path as file_path, f.project_id as project_id
            """,
            {"ids": method_ids},
        )
    return {r["id"]: r for r in recs}


def analyze_impact(store, symbol_query: str, max_depth: int = 4, project: str | None = None) -> dict:
    target_symbol_ids = _resolve_symbol_ids(store, symbol_query, project=project)
    if not target_symbol_ids:
        return {"target": symbol_query, "depth_groups": {"1": [], "2": [], "3+": []}}

    overlay_store = getattr(store, "overlay_store", None)
    if overlay_store is not None:
        methods = merged_method_records(store, overlay_store, project=project)
        symbols = merged_symbol_records(store, overlay_store, project=project)
        fqname_and_file_to_method = {
            (f"{rec.get('class_fqcn')}#{rec.get('signature')}", rec.get("file_id")): rec["id"]
            for rec in methods
        }
        symbol_to_method = {}
        for rec in symbols:
            if rec.get("kind") != "method":
                continue
            method_key = (rec.get("fqname"), rec.get("file_id"))
            method_id = fqname_and_file_to_method.get(method_key)
            if method_id:
                symbol_to_method[rec["id"]] = method_id
    else:
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
    if overlay_store is not None:
        edges = merged_call_edges(store, overlay_store, project=project)
        for edge in edges:
            edge["edge_type"] = "CALLS"
    else:
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

    # ------------------------------------------------------------------ #
    # Enrich every caller entry with human-readable metadata so AI agents
    # don't need a second round-trip to resolve raw ID hashes.
    # A single bulk query resolves all collected method IDs at once.
    # ------------------------------------------------------------------ #
    all_caller_ids = [item["symbol"] for items in depth_groups.values() for item in items]
    meta = _resolve_method_metadata(store, all_caller_ids)

    for items in depth_groups.values():
        for item in items:
            m = meta.get(item["symbol"], {})
            item["name"] = m.get("name")
            item["fqname"] = m.get("fqname")
            item["file_path"] = m.get("file_path")
            item["project_id"] = m.get("project_id")
            item["class_fqcn"] = m.get("class_fqcn")
            # Convert the call-path from a list of raw IDs to human-readable names
            # so an agent can read the chain without additional lookups.
            item["path"] = [
                meta.get(pid, {}).get("name") or pid
                for pid in item["path"]
            ]

    # Also enrich the targets_resolved list for context
    target_meta = _resolve_method_metadata(store, target_method_ids)
    resolved_targets = [
        {
            "id": mid,
            "name": target_meta.get(mid, {}).get("name"),
            "fqname": target_meta.get(mid, {}).get("fqname"),
            "file_path": target_meta.get(mid, {}).get("file_path"),
            "class_fqcn": target_meta.get(mid, {}).get("class_fqcn"),
        }
        for mid in target_method_ids
    ]

    return {
        "target": symbol_query,
        "targets_resolved": resolved_targets,
        "depth_groups": depth_groups,
        "summary": {
            "direct": len(depth_groups["1"]),
            "indirect": len(depth_groups["2"]),
            "transitive": len(depth_groups["3+"]),
        },
    }
