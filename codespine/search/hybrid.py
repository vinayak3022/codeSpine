from __future__ import annotations

from codespine.search.bm25 import rank_bm25
from codespine.search.fuzzy import rank_fuzzy
from codespine.search.rrf import reciprocal_rank_fusion
from codespine.search.vector import rank_semantic

_LOW_CONFIDENCE_THRESHOLD = 0.05


def hybrid_search(store, query: str, k: int = 20, project: str | None = None) -> list[dict]:
    project_clause = "AND f.project_id = $proj" if project else ""
    params: dict = {}
    if project:
        params["proj"] = project

    # No LIMIT — load all symbols for the scoped project so that exact class names
    # are never missing from the candidate pool (previously capped at 2000 which
    # caused exact matches on 4000+ file projects to be silently dropped).
    recs = store.query_records(
        f"""
        MATCH (s:Symbol), (f:File)
        WHERE s.file_id = f.id {project_clause}
        RETURN s.id as id,
               s.kind as kind,
               s.name as name,
               s.fqname as fqname,
               s.embedding as embedding,
               f.path as file_path,
               f.is_test as is_test
        """,
        params,
    )

    if not recs:
        return []

    query_lower = query.lower().strip()

    lexical_docs = [(r["id"], f"{r.get('name', '')} {r.get('fqname', '')}") for r in recs]
    fuzzy_docs = [(r["id"], r.get("name", "")) for r in recs]
    vector_docs = [(r["id"], r.get("embedding")) for r in recs]

    bm25_rank = rank_bm25(query, lexical_docs)
    fuzzy_rank = rank_fuzzy(query, fuzzy_docs)
    semantic_rank = rank_semantic(query, vector_docs)

    fused = reciprocal_rank_fusion([bm25_rank, semantic_rank, fuzzy_rank])
    rec_by_id = {r["id"]: r for r in recs}

    results = []
    for doc_id, score in fused:
        rec = rec_by_id.get(doc_id)
        if not rec:
            continue

        multiplier = 1.0
        if rec.get("is_test"):
            multiplier *= 0.5
        if rec.get("kind") in {"method", "class"}:
            multiplier *= 1.2

        # Exact name match: guarantee this symbol ranks first regardless of RRF score.
        name_lower = (rec.get("name") or "").lower()
        fqname_lower = (rec.get("fqname") or "").lower()
        if name_lower == query_lower or fqname_lower == query_lower:
            multiplier *= 5.0

        results.append(
            {
                "id": doc_id,
                "kind": rec.get("kind"),
                "name": rec.get("name"),
                "fqname": rec.get("fqname"),
                "file_path": rec.get("file_path"),
                "score": score * multiplier,
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    top_k = results[:k]

    # Attach architectural context in same response.
    for item in top_k:
        ctx = store.query_records(
            """
            MATCH (s:Symbol {id: $sid})-[:IN_COMMUNITY]->(c:Community)
            OPTIONAL MATCH (s)-[f:IN_FLOW]->(fl:Flow)
            RETURN c.id as community_id, c.label as community_label,
                   fl.id as flow_id, fl.kind as flow_kind, f.depth as flow_depth
            LIMIT 3
            """,
            {"sid": item["id"]},
        )
        item["context"] = ctx

    # Warn when all scores are near zero — the results are likely noise.
    if top_k and top_k[0]["score"] < _LOW_CONFIDENCE_THRESHOLD:
        for item in top_k:
            item["low_confidence"] = True
        top_k.append({
            "note": (
                "Low confidence results — all scores below threshold. "
                "If searching for an exact class or method name, use find_symbol instead."
            )
        })

    return top_k
