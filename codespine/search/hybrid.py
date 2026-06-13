from __future__ import annotations

import logging
import os

from codespine.overlay.merge import merged_symbol_records
from codespine.search.bm25 import rank_bm25
from codespine.search.fuzzy import rank_fuzzy
from codespine.search.rrf import reciprocal_rank_fusion
from codespine.search.vector import _load_model, rank_semantic

LOGGER = logging.getLogger(__name__)

_LOW_CONFIDENCE_THRESHOLD = 0.05
_SNIPPET_CONTEXT_LINES = 2  # lines above and below the symbol declaration


def _rank_trace_map(ranking: list[tuple[str, float]], limit: int) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    trace_by_id: dict[str, dict[str, object]] = {}
    traces: list[dict[str, object]] = []
    for rank, (doc_id, score) in enumerate(ranking[:limit], start=1):
        entry = {"id": doc_id, "rank": rank, "score": score}
        trace_by_id[doc_id] = entry
        traces.append(entry)
    return trace_by_id, traces


def _match_reasons(query_lower: str, rec: dict, rank_traces: dict[str, dict[str, object]]) -> list[str]:
    reasons: list[str] = []
    name_lower = (rec.get("name") or "").lower()
    fqname_lower = (rec.get("fqname") or "").lower()

    if name_lower == query_lower or fqname_lower == query_lower:
        reasons.append("exact name match")
    else:
        if query_lower and query_lower in name_lower:
            reasons.append("substring name match")
        if query_lower and query_lower in fqname_lower:
            reasons.append("substring fqname match")

    for ranker, trace in rank_traces.items():
        reasons.append(f"{ranker} rank {trace['rank']}")

    if rec.get("is_test"):
        reasons.append("test symbol penalty")
    if rec.get("kind") in {"method", "class"}:
        reasons.append("method/class boost")

    return reasons


def _confidence_reason(query_lower: str, rec: dict, rank_traces: dict[str, dict[str, object]]) -> str:
    name_lower = (rec.get("name") or "").lower()
    fqname_lower = (rec.get("fqname") or "").lower()
    if name_lower == query_lower or fqname_lower == query_lower:
        return "Exact name match"
    if query_lower and (query_lower in name_lower or query_lower in fqname_lower):
        return "Partial lexical match"
    if rank_traces:
        return "Retrieved by combined lexical, fuzzy, and semantic signals"
    return "Weak lexical overlap"


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


def _context_entry(
    community: dict | None = None,
    flow: dict | None = None,
) -> dict[str, object]:
    return {
        "community_id": community.get("community_id") if community else None,
        "community_label": community.get("community_label") if community else None,
        "flow_id": flow.get("flow_id") if flow else None,
        "flow_kind": flow.get("flow_kind") if flow else None,
        "flow_depth": flow.get("flow_depth") if flow else None,
    }


def _load_symbol_context(store, symbol_id: str) -> list[dict[str, object]]:
    community_rows = store.query_records(
        """
        MATCH (s:Symbol {id: $sid})-[:IN_COMMUNITY]->(c:Community)
        RETURN c.id as community_id, c.label as community_label
        LIMIT 3
        """,
        {"sid": symbol_id},
    )
    flow_rows = store.query_records(
        """
        MATCH (s:Symbol {id: $sid})-[f:IN_FLOW]->(fl:Flow)
        RETURN fl.id as flow_id, fl.kind as flow_kind, f.depth as flow_depth
        LIMIT 3
        """,
        {"sid": symbol_id},
    )

    context = [_context_entry(community=community) for community in community_rows]
    context.extend(_context_entry(flow=flow) for flow in flow_rows)
    return context[:3]


def hybrid_search(store, query: str, k: int = 20, project: str | None = None, explain: bool = False) -> list[dict] | dict:
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

    trace_limit = max(k, 1)
    bm25_trace_by_id, bm25_traces = _rank_trace_map(bm25_rank, trace_limit)
    fuzzy_trace_by_id, fuzzy_traces = _rank_trace_map(fuzzy_rank, trace_limit)
    semantic_trace_by_id, semantic_traces = _rank_trace_map(semantic_rank, trace_limit)

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

        rank_traces = {
            ranker: trace
            for ranker, trace in (
                ("bm25", bm25_trace_by_id.get(doc_id)),
                ("semantic", semantic_trace_by_id.get(doc_id)),
                ("fuzzy", fuzzy_trace_by_id.get(doc_id)),
            )
            if trace is not None
        }

        item = {
            "id": doc_id,
            "kind": rec.get("kind"),
            "name": rec.get("name"),
            "fqname": rec.get("fqname"),
            "file_path": rec.get("file_path"),
            "line": rec.get("line"),
            "score": score * multiplier,
        }
        if explain:
            item["retrieval_traces"] = rank_traces
            item["match_reasons"] = _match_reasons(query_lower, rec, rank_traces)
            item["confidence_reason"] = _confidence_reason(query_lower, rec, rank_traces)
        results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)
    top_k = results[:k]

    # Attach architectural context in the same response. This is best-effort:
    # a context query failure must not hide ranked symbol results.
    for item in top_k:
        try:
            item["context"] = _load_symbol_context(store, item["id"])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Unable to load architectural context for %s: %s", item.get("id"), exc)
            item["context"] = []
            item["context_warning"] = "Architectural context unavailable for this result."

    # Attach source code snippets (3–5 lines around the declaration) to the
    # top results so agents have immediate context without reading the file.
    for item in top_k:
        if isinstance(item, dict) and item.get("file_path") and item.get("line"):
            snippet = _read_snippet(item["file_path"], int(item["line"]))
            if snippet:
                item["snippet"] = snippet

    # FR-10: Calibrate confidence labels based on name matching, not just score.
    # Exact name match → "high"; partial match → "medium"; no match → "low".
    # This prevents exact-match results being incorrectly labelled "low_confidence"
    # when the embedding model is not installed.
    has_exact_match = False
    for item in top_k:
        if not isinstance(item, dict) or "score" not in item:
            continue
        item_name = (item.get("name") or "").lower()
        item_fqname = (item.get("fqname") or "").lower()
        if item_name == query_lower or item_fqname == query_lower:
            item["confidence"] = "high"
            has_exact_match = True
        elif query_lower in item_name or query_lower in item_fqname:
            item["confidence"] = "medium"
        else:
            item["confidence"] = "low"

    low_confidence_note: str | None = None

    # Only add low-confidence warning when there are no exact matches AND all
    # RRF scores are below the noise threshold.
    if not has_exact_match and top_k and isinstance(top_k[0], dict) and top_k[0].get("score", 1.0) < _LOW_CONFIDENCE_THRESHOLD:
        has_model = _load_model() is not None
        for item in top_k:
            if isinstance(item, dict) and "score" in item:
                item["low_confidence"] = True
        if has_model:
            low_confidence_note = (
                "Low confidence results — all scores below threshold. "
                "If searching for an exact class or method name, use find_symbol instead."
            )
        else:
            low_confidence_note = (
                "Low confidence results — scores are lower in BM25/fuzzy-only mode "
                "(no embedding model detected). "
                "This is expected without 'codespine[ml]' installed; results may still be correct. "
                "For exact name matches, use find_symbol instead."
            )

    if not explain:
        return top_k

    payload = {
        "retrieval_mode": "hybrid",
        "query": query,
        "results": top_k,
        "provenance": {
            "rankers": {
                "bm25": {"traces": bm25_traces},
                "semantic": {"traces": semantic_traces},
                "fuzzy": {"traces": fuzzy_traces},
            }
        },
    }
    if low_confidence_note:
        payload["note"] = low_confidence_note
    return payload
