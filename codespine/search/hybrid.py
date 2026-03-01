from __future__ import annotations

from codespine.config import SETTINGS
from codespine.search.bm25 import rank_bm25
from codespine.search.fuzzy import rank_fuzzy
from codespine.search.rrf import reciprocal_rank_fusion
from codespine.search.vector import rank_semantic


def hybrid_search(store, query: str, k: int = 20) -> list[dict]:
    recs = store.query_records(
        """
        MATCH (s:Symbol), (f:File {id: s.file_id})
        RETURN s.id as id,
               s.kind as kind,
               s.name as name,
               s.fqname as fqname,
               s.embedding as embedding,
               f.path as file_path,
               f.is_test as is_test
        LIMIT $lim
        """,
        {"lim": SETTINGS.semantic_candidate_pool},
    )

    if not recs:
        return []

    lexical_docs = [(r["id"], f"{r.get('name', '')} {r.get('fqname', '')}") for r in recs]
    fuzzy_docs = [(r["id"], r.get("name", "")) for r in recs]
    vector_docs = [(r["id"], r.get("embedding")) for r in recs]

    bm25_rank = rank_bm25(query, lexical_docs)
    fuzzy_rank = rank_fuzzy(query, fuzzy_docs)
    semantic_rank = rank_semantic(query, vector_docs)

    fused = reciprocal_rank_fusion([bm25_rank, semantic_rank, fuzzy_rank], k=SETTINGS.rrf_k)
    rec_by_id = {r["id"]: r for r in recs}

    results = []
    for doc_id, score in fused[: max(k * 3, k)]:
        rec = rec_by_id.get(doc_id)
        if not rec:
            continue

        multiplier = 1.0
        if rec.get("is_test"):
            multiplier *= 0.5
        if rec.get("kind") in {"method", "class"}:
            multiplier *= 1.2

        final_score = score * multiplier
        results.append(
            {
                "id": doc_id,
                "kind": rec.get("kind"),
                "name": rec.get("name"),
                "fqname": rec.get("fqname"),
                "file_path": rec.get("file_path"),
                "score": final_score,
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)

    # Attach architectural context in same response.
    for item in results[:k]:
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

    return results[:k]
