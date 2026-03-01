from __future__ import annotations

from collections import defaultdict, deque


def _entry_methods(store) -> list[str]:
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
    fallback = store.query_records(
        """
        MATCH (m:Method)
        WITH m ORDER BY m.name LIMIT 10
        RETURN m.id as id
        """
    )
    return [r["id"] for r in fallback]


def trace_execution_flows(store, entry_symbol: str | None = None, max_depth: int = 6) -> list[dict]:
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
        start = store.query_records(
            """
            MATCH (m:Method)
            WHERE m.id = $q OR lower(m.name) = lower($q) OR lower(m.signature) CONTAINS lower($q)
            RETURN m.id as id
            LIMIT 10
            """,
            {"q": entry_symbol},
        )
        entries = [r["id"] for r in start]
    else:
        entries = _entry_methods(store)

    flows = []
    for e in entries:
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

    return flows
