from __future__ import annotations

import os

from codespine.overlay.merge import merged_symbol_records
from codespine.search.bm25 import rank_bm25
from codespine.search.fuzzy import rank_fuzzy
from codespine.search.rrf import reciprocal_rank_fusion
from codespine.search.vector import _load_model, rank_semantic

_LOW_CONFIDENCE_THRESHOLD = 0.05
_SNIPPET_CONTEXT_LINES = 2  # lines above and below the symbol declaration


def _read_snippet(file_path: str, line: int, context: int = _SNIPPET_CONTEXT_LINES) -> str | None:
    """Best-effort extraction of source lines around a symbol declaration."""
    if not file_path or not line or line < 1:
        return None
    try:
        if not os.path.isfile(file_path):
            return None
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
        start = max(0, line - 1 - context)
        end = min(len(all_lines), line + context)
        snippet_lines = all_lines[start:end]
        return "".join(snippet_lines).rstrip("\n")
    except Exception:
        return None


def hybrid_search(store, query: str, k: int = 20, project: str | None = None) -> list[dict]:
    overlay_store = getattr(store, "overlay_store", None)
    if overlay_store is not None:
        recs = merged_symbol_records(store, overlay_store, project=project)
    else:
        project_clause = "AND f.project_id = $proj" if project else ""
        params: dict = {}
        if project:
            params["proj"] = project
        recs = store.query_records(
            f"""
            MATCH (s:Symbol), (f:File)
            WHERE s.file_id = f.id {project_clause}
            RETURN s.id as id,
                   s.kind as kind,
                   s.name as name,
                   s.fqname as fqname,
                   s.embedding as embedding,
                   s.line as line,
                   s.file_id as file_id,
                   f.path as file_path,
                   f.project_id as project_id,
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
                "line": rec.get("line"),
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

    # Attach source code snippets (3–5 lines around the declaration) to the
    # top results so agents have immediate context without reading the file.
    for item in top_k:
        if isinstance(item, dict) and item.get("file_path") and item.get("line"):
            snippet = _read_snippet(item["file_path"], int(item["line"]))
            if snippet:
                item["snippet"] = snippet

    # Warn when all scores are near zero — the results are likely noise.
    # The threshold 0.05 is calibrated for embedding mode.  Without sentence-
    # transformers the hash-fallback vector and BM25/fuzzy signals produce lower
    # RRF scores, so the warning fires on nearly every query.  Make the note
    # context-aware so the agent understands whether this is a calibration issue
    # or a genuine low-relevance result.
    if top_k and top_k[0]["score"] < _LOW_CONFIDENCE_THRESHOLD:
        has_model = _load_model() is not None
        for item in top_k:
            item["low_confidence"] = True
        if has_model:
            note = (
                "Low confidence results — all scores below threshold. "
                "If searching for an exact class or method name, use find_symbol instead."
            )
        else:
            note = (
                "Low confidence results — scores are lower in BM25/fuzzy-only mode "
                "(no embedding model detected). "
                "This is expected without 'codespine[ml]' installed; results may still be correct. "
                "For exact name matches, use find_symbol instead."
            )
        top_k.append({"note": note})

    return top_k
