from __future__ import annotations

from collections import defaultdict


def detect_communities(store, progress=None) -> list[dict]:
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    _ping("loading symbols")
    symbols = store.query_records("MATCH (s:Symbol) RETURN s.id as id, s.fqname as fqname")
    _ping(f"{len(symbols)} symbols, loading edges")
    edges = store.query_records(
        """
        MATCH (a:Method)-[:CALLS]->(b:Method)
        RETURN a.id as src, b.id as dst
        """
    )
    if not symbols:
        return []

    ids = [s["id"] for s in symbols]
    index_of = {sid: i for i, sid in enumerate(ids)}

    _ping(f"{len(edges)} edges, clustering")
    membership: dict[str, int] = {}
    try:
        import igraph as ig
        import leidenalg

        g = ig.Graph(directed=False)
        g.add_vertices(len(ids))
        graph_edges = []
        for e in edges:
            if e["src"] in index_of and e["dst"] in index_of:
                graph_edges.append((index_of[e["src"]], index_of[e["dst"]]))
        if graph_edges:
            g.add_edges(graph_edges)
        part = leidenalg.find_partition(g, leidenalg.ModularityVertexPartition)
        for idx, cid in enumerate(part.membership):
            membership[ids[idx]] = int(cid)
    except Exception:
        # Fallback: group by package prefix from fqname.
        for s in symbols:
            fq = s.get("fqname") or ""
            key = fq.rsplit(".", 2)[0] if "." in fq else fq
            membership[s["id"]] = abs(hash(key)) % 10000

    grouped: dict[int, list[str]] = defaultdict(list)
    for sid, cid in membership.items():
        grouped[cid].append(sid)

    _ping(f"{len(grouped)} clusters, persisting")
    communities: list[dict] = []
    done_clusters = 0
    total_clusters = len(grouped)
    for cid, symbol_ids in grouped.items():
        cohesion = 1.0 / max(len(symbol_ids), 1)
        label = f"community_{cid}"
        store.set_community(str(cid), label, cohesion, symbol_ids)
        done_clusters += 1
        if done_clusters % 200 == 0 or done_clusters == total_clusters:
            _ping(f"persisting {done_clusters}/{total_clusters} clusters")
        communities.append(
            {
                "community_id": str(cid),
                "label": label,
                "cohesion": cohesion,
                "size": len(symbol_ids),
            }
        )

    communities.sort(key=lambda c: c["size"], reverse=True)
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
