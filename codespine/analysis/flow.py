from __future__ import annotations

from collections import defaultdict, deque

from codespine.analysis.impact import _resolve_method_metadata


def _sorted_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _resolve_entry_methods(store, entry_symbol: str, project: str | None = None) -> list[str]:
    needle = (entry_symbol or "").strip()
    if not needle:
        return []

    def _ids(rows: list[dict]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for row in rows:
            mid = str(row.get("id") or "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out.append(mid)
        return out

    if project:
        project_clause = "AND f.project_id = $proj"
        params: dict = {"q": needle, "proj": project}
    else:
        project_clause = ""
        params = {"q": needle}

    exact = store.query_records(
        f"""
        MATCH (m:Method), (c:Class), (f:File)
        WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
          AND (m.id = $q OR lower(m.name) = lower($q) OR lower(m.signature) = lower($q))
        RETURN m.id as id
        LIMIT 10
        """,
        params,
    )
    ids = _ids(exact)
    if ids:
        return ids

    if "#" in needle:
        class_fqcn, member = needle.rsplit("#", 1)
        class_fqcn = class_fqcn.strip()
        member = member.strip()
        if not class_fqcn or not member:
            return []
        params = {"class_fqcn": class_fqcn, "member": member}
        if project:
            params["proj"] = project
        scoped = store.query_records(
            f"""
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
              AND lower(c.fqcn) = lower($class_fqcn)
              AND (lower(m.name) = lower($member) OR lower(m.signature) = lower($member))
            RETURN m.id as id
            LIMIT 10
            """,
            params,
        )
        return _ids(scoped)
    return []


def _entry_methods(store, project: str | None = None) -> list[str]:
    if project:
        recs = store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $proj
            AND (m.name = 'main' OR m.is_test = true)
            RETURN m.id as id
            """,
            {"proj": project},
        )
    else:
        recs = store.query_records(
            """
            MATCH (m:Method)
            WHERE m.name = 'main' OR m.is_test = true
            RETURN m.id as id
            """
        )
    ids = [r["id"] for r in recs]
    if ids:
        return ids
    if project:
        fallback = store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $proj
            WITH m ORDER BY m.name LIMIT 10
            RETURN m.id as id
            """,
            {"proj": project},
        )
    else:
        fallback = store.query_records(
            """
            MATCH (m:Method)
            WITH m ORDER BY m.name LIMIT 10
            RETURN m.id as id
            """
        )
    return [r["id"] for r in fallback]


def _trace_execution_flows_limited(
    store,
    entry_symbol: str | None = None,
    max_depth: int = 6,
    project: str | None = None,
    progress=None,
    entry_point_cap: int = 25,
    per_flow_node_cap: int = 25,
    total_node_cap: int = 200,
) -> dict:
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    _ping("loading call graph")
    if project:
        edges = store.query_records(
            """
            MATCH (a:Method)-[:CALLS]->(b:Method), (ca:Class), (fa:File), (cb:Class), (fb:File)
            WHERE a.class_id = ca.id AND ca.file_id = fa.id
              AND b.class_id = cb.id AND cb.file_id = fb.id
              AND fa.project_id = $proj AND fb.project_id = $proj
            RETURN a.id as src, b.id as dst
            """,
            {"proj": project},
        )
    else:
        edges = store.query_records(
            """
            MATCH (a:Method)-[:CALLS]->(b:Method)
            RETURN a.id as src, b.id as dst
            """
        )
    adj: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adj[edge["src"]].append(edge["dst"])
    for src in list(adj):
        adj[src] = sorted(_sorted_unique(adj[src]))

    if entry_symbol:
        entries = _resolve_entry_methods(store, entry_symbol, project=project)
    else:
        entries = _entry_methods(store, project=project)
    entries = sorted(_sorted_unique(entries))

    entry_cap = max(0, int(entry_point_cap))
    per_flow_cap = max(0, int(per_flow_node_cap))
    total_cap = max(0, int(total_node_cap))
    flows: list[dict] = []
    total_nodes_emitted = 0
    entry_points_truncated = len(entries) > entry_cap
    total_node_cap_reached = False

    _ping(f"{min(len(entries), entry_cap)} entry points, tracing")
    for idx, e in enumerate(entries[:entry_cap]):
        if total_nodes_emitted >= total_cap:
            total_node_cap_reached = True
            break
        if idx % 50 == 0 and idx > 0:
            _ping(f"traced {idx}/{min(len(entries), entry_cap)} entry points")

        visited = {e}
        q = deque([(e, 0)])
        nodes_with_depth = [(e, 0)] if per_flow_cap > 0 and total_cap > 0 else []
        if nodes_with_depth:
            total_nodes_emitted += 1

        flow_truncated = False
        while q:
            node, depth = q.popleft()
            if depth >= max_depth:
                continue
            for nxt in adj.get(node, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                if len(nodes_with_depth) >= per_flow_cap:
                    flow_truncated = True
                    break
                if total_nodes_emitted >= total_cap:
                    flow_truncated = True
                    total_node_cap_reached = True
                    break
                q.append((nxt, depth + 1))
                nodes_with_depth.append((nxt, depth + 1))
                total_nodes_emitted += 1
            if flow_truncated:
                break

        if not nodes_with_depth:
            flow_truncated = True
            continue

        flows.append(
            {
                "entry": e,
                "kind": "cross_community" if len(nodes_with_depth) > 12 else "intra_community",
                "nodes": [{"symbol": n, "depth": d} for n, d in nodes_with_depth],
                "truncated": flow_truncated,
            }
        )

    _ping(f"{len(flows)} flows, enriching metadata")
    all_ids = list({node["symbol"] for flow in flows for node in flow["nodes"]})
    meta = _resolve_method_metadata(store, all_ids, project=project)

    for flow in flows:
        entry_m = meta.get(flow["entry"], {})
        flow["entry_name"] = entry_m.get("name")
        flow["entry_fqname"] = entry_m.get("fqname")
        flow["entry_file_path"] = entry_m.get("file_path")
        for node in flow["nodes"]:
            m = meta.get(node["symbol"], {})
            node["name"] = m.get("name")
            node["fqname"] = m.get("fqname")
            node["file_path"] = m.get("file_path")
            node["project_id"] = m.get("project_id")

    truncation = {
        "entry_point_cap": entry_cap,
        "per_flow_node_cap": per_flow_cap,
        "total_node_cap": total_cap,
        "entry_points_found": len(entries),
        "entry_points_emitted": len(flows),
        "entry_points_truncated": entry_points_truncated or total_node_cap_reached,
        "total_nodes_emitted": total_nodes_emitted,
        "total_node_cap_reached": total_node_cap_reached,
        "flows_truncated": any(flow.get("truncated") for flow in flows) or entry_points_truncated or total_node_cap_reached or len(flows) < len(entries[:entry_cap]),
    }
    return {"flows": flows, "truncation": truncation}


def trace_execution_flows(
    store,
    entry_symbol: str | None = None,
    max_depth: int = 6,
    project: str | None = None,
    progress=None,
    *,
    include_metadata: bool = False,
    entry_point_cap: int = 25,
    per_flow_node_cap: int = 25,
    total_node_cap: int = 200,
) -> list[dict] | dict:
    if include_metadata:
        return _trace_execution_flows_limited(
            store,
            entry_symbol=entry_symbol,
            max_depth=max_depth,
            project=project,
            progress=progress,
            entry_point_cap=entry_point_cap,
            per_flow_node_cap=per_flow_node_cap,
            total_node_cap=total_node_cap,
        )

    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    _ping("loading call graph")
    if project:
        edges = store.query_records(
            """
            MATCH (a:Method)-[:CALLS]->(b:Method), (ca:Class), (fa:File), (cb:Class), (fb:File)
            WHERE a.class_id = ca.id AND ca.file_id = fa.id
              AND b.class_id = cb.id AND cb.file_id = fb.id
              AND fa.project_id = $proj AND fb.project_id = $proj
            RETURN a.id as src, b.id as dst
            """,
            {"proj": project},
        )
    else:
        edges = store.query_records(
            """
            MATCH (a:Method)-[:CALLS]->(b:Method)
            RETURN a.id as src, b.id as dst
            """
        )
    adj: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adj[edge["src"]].append(edge["dst"])

    if entry_symbol:
        entries = _resolve_entry_methods(store, entry_symbol, project=project)
    else:
        entries = _entry_methods(store, project=project)

    _ping(f"{len(entries)} entry points, tracing")
    flows = []
    for idx, e in enumerate(entries):
        if idx % 50 == 0 and idx > 0:
            _ping(f"traced {idx}/{len(entries)} entry points")
        visited = {e}
        q = deque([(e, 0)])
        nodes_with_depth = [(e, 0)]

        while q:
            node, depth = q.popleft()
            if depth >= max_depth:
                continue
            for nxt in adj.get(node, []):
                if nxt in visited:
                    continue
                visited.add(nxt)
                q.append((nxt, depth + 1))
                nodes_with_depth.append((nxt, depth + 1))

        flows.append(
            {
                "entry": e,
                "kind": "cross_community" if len(nodes_with_depth) > 12 else "intra_community",
                "nodes": [{"symbol": n, "depth": d} for n, d in nodes_with_depth],
            }
        )

    # ------------------------------------------------------------------ #
    # Enrich every node with human-readable metadata so AI agents don't
    # need a second round-trip to resolve raw method ID hashes.
    # Collect all unique IDs across all flows, resolve in one bulk query.
    # ------------------------------------------------------------------ #
    _ping(f"{len(flows)} flows, enriching metadata")
    all_ids = list({node["symbol"] for flow in flows for node in flow["nodes"]})
    meta = _resolve_method_metadata(store, all_ids, project=project)

    for flow in flows:
        entry_m = meta.get(flow["entry"], {})
        flow["entry_name"] = entry_m.get("name")
        flow["entry_fqname"] = entry_m.get("fqname")
        flow["entry_file_path"] = entry_m.get("file_path")
        for node in flow["nodes"]:
            m = meta.get(node["symbol"], {})
            node["name"] = m.get("name")
            node["fqname"] = m.get("fqname")
            node["file_path"] = m.get("file_path")
            node["project_id"] = m.get("project_id")

    return flows
