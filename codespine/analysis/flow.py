from __future__ import annotations

from collections import defaultdict, deque

from codespine.analysis.impact import _resolve_method_metadata


def _resolve_entry_methods(store, entry_symbol: str, project: str | None = None) -> list[str]:
    needle = (entry_symbol or "").strip()
    if not needle:
        return []

    candidates: list[str] = []

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    _add(needle)
    if "#" in needle:
        _add(needle.rsplit("#", 1)[1])
    if "(" in needle and needle.endswith(")"):
        _add(needle.split("(", 1)[0])

    for candidate in candidates:
        if project:
            start = store.query_records(
                """
                MATCH (m:Method), (c:Class), (f:File)
                WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $proj
                AND (m.id = $q OR lower(m.name) = lower($q) OR lower(m.signature) = lower($q) OR lower(m.signature) CONTAINS lower($q))
                RETURN m.id as id
                LIMIT 10
                """,
                {"q": candidate, "proj": project},
            )
        else:
            start = store.query_records(
                """
                MATCH (m:Method)
                WHERE m.id = $q OR lower(m.name) = lower($q) OR lower(m.signature) = lower($q) OR lower(m.signature) CONTAINS lower($q)
                RETURN m.id as id
                LIMIT 10
                """,
                {"q": candidate},
            )
        ids = [r["id"] for r in start]
        if ids:
            return ids
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


def trace_execution_flows(store, entry_symbol: str | None = None, max_depth: int = 6, project: str | None = None, progress=None) -> list[dict]:
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
    meta = _resolve_method_metadata(store, all_ids)

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
