from __future__ import annotations

from collections import Counter, defaultdict

MAX_LEIDEN_SYMBOLS = 12000
MIN_COMMUNITY_SIZE = 2
PACKAGE_BUCKET_DEPTH = 5


def _package_bucket(fqname: str) -> str:
    base = (fqname or "").split("#", 1)[0]
    parts = [p for p in base.split(".") if p]
    if len(parts) <= 2:
        return base or "default"
    package_parts = parts[:-1] if len(parts) > 1 else parts
    depth = min(PACKAGE_BUCKET_DEPTH, len(package_parts))
    return ".".join(package_parts[:depth]) or base or "default"


def _community_label(symbol_ids: list[str], symbol_meta: dict[str, dict]) -> str:
    bucket_counts = Counter(_package_bucket(symbol_meta[sid].get("fqname", "")) for sid in symbol_ids if sid in symbol_meta)
    if bucket_counts:
        return bucket_counts.most_common(1)[0][0]
    return "community"


def _call_graph_communities(symbol_meta: dict[str, dict], method_edges: list[tuple[str, str]], progress=None) -> dict[str, int]:
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    graph_nodes = sorted({sid for edge in method_edges for sid in edge})
    if not graph_nodes:
        return {}

    if len(graph_nodes) > MAX_LEIDEN_SYMBOLS:
        _ping(f"graph too large for leiden ({len(graph_nodes)} symbols), using package fallback")
        return {}

    index_of = {sid: i for i, sid in enumerate(graph_nodes)}
    membership: dict[str, int] = {}
    try:
        import igraph as ig
        import leidenalg

        _ping(f"{len(graph_nodes)} connected symbols, running leiden")
        g = ig.Graph(directed=False)
        g.add_vertices(len(graph_nodes))
        g.add_edges([(index_of[src], index_of[dst]) for src, dst in method_edges if src in index_of and dst in index_of])
        part = leidenalg.find_partition(g, leidenalg.ModularityVertexPartition)
        for idx, cid in enumerate(part.membership):
            membership[graph_nodes[idx]] = int(cid)
    except Exception:
        _ping("leiden unavailable, using package fallback")
        return {}
    return membership


def detect_communities(store, progress=None) -> list[dict]:
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    _ping("loading symbols")
    symbols = store.query_records(
        """
        MATCH (s:Symbol)
        RETURN s.id as id, s.kind as kind, s.fqname as fqname, s.file_id as file_id
        """
    )
    if not symbols:
        return []

    symbol_meta = {s["id"]: s for s in symbols}
    method_symbols_by_key: dict[tuple[str, str], str] = {}
    class_symbols_by_key: dict[tuple[str, str], str] = {}
    for symbol in symbols:
        key = (symbol.get("file_id", ""), symbol.get("fqname", ""))
        if symbol.get("kind") == "method":
            method_symbols_by_key[key] = symbol["id"]
        elif symbol.get("kind") == "class":
            class_symbols_by_key[key] = symbol["id"]

    _ping("loading methods")
    method_rows = store.query_records(
        """
        MATCH (m:Method), (c:Class)
        WHERE m.class_id = c.id
        RETURN m.id as method_id, c.file_id as file_id, c.fqcn as class_fqcn, m.signature as signature
        """
    )
    method_symbol_ids: dict[str, str] = {}
    graph_edges: set[tuple[str, str]] = set()
    for row in method_rows:
        file_id = row.get("file_id", "")
        fqname = f"{row.get('class_fqcn', '')}#{row.get('signature', '')}"
        method_symbol_id = method_symbols_by_key.get((file_id, fqname))
        if not method_symbol_id:
            continue
        method_symbol_ids[row["method_id"]] = method_symbol_id
        class_symbol_id = class_symbols_by_key.get((file_id, row.get("class_fqcn", "")))
        if class_symbol_id and class_symbol_id != method_symbol_id:
            graph_edges.add(tuple(sorted((method_symbol_id, class_symbol_id))))

    _ping("loading call edges")
    call_rows = store.query_records(
        """
        MATCH (a:Method)-[:CALLS]->(b:Method)
        RETURN a.id as src, b.id as dst
        """
    )
    for row in call_rows:
        src = method_symbol_ids.get(row.get("src", ""))
        dst = method_symbol_ids.get(row.get("dst", ""))
        if src and dst and src != dst:
            graph_edges.add(tuple(sorted((src, dst))))

    _ping(f"{len(symbols)} symbols, {len(graph_edges)} structural edges")
    membership = _call_graph_communities(symbol_meta, sorted(graph_edges), progress=progress)

    grouped: dict[str, list[str]] = defaultdict(list)
    next_fallback_id = 1000000

    # Keep only meaningful graph communities; tiny ones get merged by package bucket below.
    temp_grouped: dict[int, list[str]] = defaultdict(list)
    for sid, cid in membership.items():
        temp_grouped[cid].append(sid)

    for cid, members in temp_grouped.items():
        if len(members) >= MIN_COMMUNITY_SIZE:
            grouped[f"graph:{cid}"].extend(members)
        else:
            for sid in members:
                grouped[f"pkg:{_package_bucket(symbol_meta[sid].get('fqname', ''))}"].append(sid)

    for sid, meta in symbol_meta.items():
        if sid in membership:
            continue
        grouped[f"pkg:{_package_bucket(meta.get('fqname', ''))}"].append(sid)

    # Filter out residual singletons. They are not useful architectural communities.
    filtered = {cid: members for cid, members in grouped.items() if len(members) >= MIN_COMMUNITY_SIZE}
    if not filtered:
        # Last resort: put everything into one broad bucket so callers still get context.
        cid = f"fallback:{next_fallback_id}"
        filtered[cid] = list(symbol_meta.keys())

    _ping(f"{len(filtered)} clusters, replacing previous communities")
    store.clear_communities()

    communities: list[dict] = []
    total_clusters = len(filtered)
    for idx, (cid, symbol_ids) in enumerate(sorted(filtered.items()), start=1):
        label = _community_label(symbol_ids, symbol_meta)
        cohesion = min(1.0, len(symbol_ids) / max(len(symbol_meta), 1))
        store.set_community(cid, label, cohesion, symbol_ids)
        if idx % 100 == 0 or idx == total_clusters:
            _ping(f"persisting {idx}/{total_clusters} clusters")
        communities.append(
            {
                "community_id": cid,
                "label": label,
                "cohesion": cohesion,
                "size": len(symbol_ids),
            }
        )

    communities.sort(key=lambda c: (c["size"], c["label"]), reverse=True)
    return communities


def symbol_community(store, symbol_query: str) -> dict:
    recs = store.query_records(
        """
        MATCH (s:Symbol)-[:IN_COMMUNITY]->(c:Community)
        WHERE s.id = $q OR lower(s.fqname) = lower($q) OR lower(s.name) = lower($q)
        RETURN s.id as symbol_id, s.fqname as fqname, c.id as community_id, c.label as label, c.cohesion as cohesion
        LIMIT 20
        """,
        {"q": symbol_query},
    )
    return {"query": symbol_query, "matches": recs}
