from __future__ import annotations

import logging
import os

from codespine import __version__
from codespine.config import SETTINGS
from codespine.overlay.merge import _load_overlay_docs, merged_symbol_records
from codespine.search.bm25 import rank_bm25
from codespine.search.fuzzy import rank_fuzzy
from codespine.search.rrf import reciprocal_rank_fusion
from codespine.search.vector import _load_model, rank_cross_encoder, rank_semantic, rank_semantic_sql

LOGGER = logging.getLogger(__name__)

# Cross-encoder rerank is applied to the top N candidates from RRF fusion.
_CROSS_ENCODER_RERANK_TOP_N = 20

_LOW_CONFIDENCE_THRESHOLD = 0.05
_SNIPPET_CONTEXT_LINES = 2  # lines above and below the symbol declaration
_SEARCH_PROVENANCE_VERSION = 12

# Stopwords stripped before token-level confidence matching.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "can", "could", "will",
    "would", "shall", "should", "may", "might", "must", "of", "in",
    "on", "at", "to", "for", "with", "by", "from", "as", "into",
    "how", "what", "when", "where", "why", "who", "which", "this",
    "that", "it", "its", "or", "and", "but", "not", "no", "if",
    "about", "does", "just", "very", "than", "also", "so", "get", "got",
})


def _store_snapshot_mtime(store, project: str | None = None) -> float:
    try:
        router = getattr(store, "router", None)
        if router is not None and hasattr(router, "all_shards") and hasattr(router, "snapshot_path"):
            shard_ids = list(router.all_shards())
            mtimes = [_snapshot_mtime_for_path(router.snapshot_path(idx) + ".updated") for idx in shard_ids]
            return max(mtimes, default=0.0)
        snapshot_path = getattr(store, "_snapshot_path", "")
        return _snapshot_mtime_for_path(snapshot_path + ".updated")
    except Exception:
        return 0.0


def _overlay_snapshot_mtime(store, project: str | None = None) -> float:
    overlay_store = getattr(store, "overlay_store", None)
    if overlay_store is None:
        return 0.0
    try:
        if project:
            return _snapshot_mtime_for_path(overlay_store.project_path(project))
        mtimes = []
        for doc in overlay_store.list_projects():
            project_id = doc.get("project_id")
            if project_id:
                mtimes.append(_snapshot_mtime_for_path(overlay_store.project_path(project_id)))
        return max(mtimes, default=0.0)
    except Exception:
        return 0.0


def _snapshot_mtime_for_path(path: str) -> float:
    try:
        if path and os.path.exists(path):
            return os.stat(path).st_mtime_ns / 1_000_000_000
    except OSError:
        pass
    return 0.0


def _sql_prefilter_candidates(
    store, query_lower: str, pool_size: int, project: str | None = None,
) -> list[dict]:
    """Pre-filter the candidate symbol pool using SQL CONTAINS.

    Runs a DuckDB-native CONTAINS query against symbol name/fqname to quickly
    narrow the candidate pool before expensive in-Python BM25/fuzzy ranking.
    Produces a dict of symbol records (without embeddings) for the top-*pool_size*
    matches.

    Falls back to returning the first *pool_size* records if the SQL path fails.
    """
    pref_clause = "AND f.project_id = $proj" if project else ""
    pref_params: dict = {"q": query_lower, "lim": pool_size}
    if project:
        pref_params["proj"] = project
    rows = store.query_records(
        f"""
        MATCH (s:Symbol), (f:File)
        WHERE s.file_id = f.id {pref_clause}
          AND (lower(s.name) CONTAINS $q OR lower(s.fqname) CONTAINS $q)
        RETURN s.id as id, s.kind as kind, s.name as name, s.fqname as fqname,
               s.line as line, s.file_id as file_id,
               f.path as file_path, f.project_id as project_id, f.is_test as is_test
        LIMIT $lim
        """,
        pref_params,
    )
    return rows if rows else []


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
    id_lower = (rec.get("id") or "").lower()
    name_lower = (rec.get("name") or "").lower()
    fqname_lower = (rec.get("fqname") or "").lower()

    if id_lower == query_lower:
        reasons.append("exact id match")
    elif name_lower == query_lower or fqname_lower == query_lower:
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
    id_lower = (rec.get("id") or "").lower()
    name_lower = (rec.get("name") or "").lower()
    fqname_lower = (rec.get("fqname") or "").lower()
    if id_lower == query_lower:
        return "Exact id match"
    if name_lower == query_lower or fqname_lower == query_lower:
        return "Exact name match"
    if query_lower and (query_lower in name_lower or query_lower in fqname_lower):
        return "Partial lexical match"
    if rank_traces:
        return "Retrieved by combined lexical, fuzzy, and semantic signals"
    return "Weak lexical overlap"


def _is_exact_query_match(query_lower: str, rec: dict) -> bool:
    if not query_lower:
        return False
    return query_lower in {
        (rec.get("id") or "").lower(),
        (rec.get("name") or "").lower(),
        (rec.get("fqname") or "").lower(),
    }


def _exact_match_sort_key(query_lower: str, rec: dict) -> tuple[int, int, int, str, str, str]:
    id_lower = (rec.get("id") or "").lower()
    name_lower = (rec.get("name") or "").lower()
    fqname_lower = (rec.get("fqname") or "").lower()
    kind = (rec.get("kind") or "").lower()
    exact_match_rank = 0 if query_lower in {id_lower, fqname_lower} else 1
    kind_rank = 0 if kind == "class" else 1 if kind == "method" else 2
    test_rank = 1 if rec.get("is_test") else 0
    return (exact_match_rank, kind_rank, test_rank, fqname_lower, name_lower, id_lower)


def _build_lexical_text(rec: dict) -> str:
    """Build a rich lexical search text for BM25 matching.

    Includes kind, name, fqname, file-path basename, and project ID so that
    BM25 matches on any of these dimensions without requiring exact fqname.
    """
    name = rec.get("name") or ""
    fqname = rec.get("fqname") or ""
    kind = rec.get("kind") or ""
    file_path = rec.get("file_path") or ""
    project_id = rec.get("project_id") or ""
    path_base = file_path.rsplit("/", 1)[-1].replace(".java", "") if file_path else ""
    parts = [name, fqname, kind]
    if path_base and path_base not in (name, fqname):
        parts.append(path_base)
    if project_id:
        parts.append(project_id)
    return " ".join(parts)


def _build_embedding_text(rec: dict) -> str:
    """Build a structured embedding text from a symbol record.

    Structured templates produce better vector representations than raw
    identifiers alone, especially for sentence-transformer models.
    """
    name = rec.get("name") or ""
    fqname = rec.get("fqname") or ""
    kind = rec.get("kind") or ""
    file_path = rec.get("file_path") or ""
    project_id = rec.get("project_id") or ""
    return (
        f"type: {kind}, name: {name}, qualified name: {fqname}, "
        f"file: {file_path.rsplit('/', 1)[-1] if file_path else ''}, "
        f"project: {project_id}"
    )


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


def _dirty_overlay_file_paths(overlay_store, project: str | None = None) -> set[str]:
    paths: set[str] = set()
    for doc in _load_overlay_docs(overlay_store, project):
        for file_path in (doc.get("dirty_files") or {}).keys():
            paths.add(file_path)
    return paths


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


def _load_symbol_contexts(store, symbol_ids: list[str]) -> dict[str, list[dict[str, object]]]:
    unique_ids = list(dict.fromkeys(symbol_ids))
    if not unique_ids:
        return {}

    community_rows = store.query_records(
        """
        MATCH (s:Symbol)-[:IN_COMMUNITY]->(c:Community)
        WHERE s.id IN $sids
        RETURN s.id as symbol_id, c.id as community_id, c.label as community_label
        ORDER BY s.id, c.id
        """,
        {"sids": unique_ids},
    )
    flow_rows = store.query_records(
        """
        MATCH (s:Symbol)-[f:IN_FLOW]->(fl:Flow)
        WHERE s.id IN $sids
        RETURN s.id as symbol_id, fl.id as flow_id, fl.kind as flow_kind, f.depth as flow_depth
        ORDER BY s.id, fl.id
        """,
        {"sids": unique_ids},
    )

    communities: dict[str, list[dict[str, object]]] = {}
    flows: dict[str, list[dict[str, object]]] = {}
    for row in community_rows:
        symbol_id = row.get("symbol_id")
        if symbol_id:
            communities.setdefault(str(symbol_id), []).append(_context_entry(community=row))
    for row in flow_rows:
        symbol_id = row.get("symbol_id")
        if symbol_id:
            flows.setdefault(str(symbol_id), []).append(_context_entry(flow=row))

    contexts: dict[str, list[dict[str, object]]] = {}
    for symbol_id in unique_ids:
        context = communities.get(symbol_id, []) + flows.get(symbol_id, [])
        contexts[symbol_id] = context[:3]
    return contexts


def hybrid_search(
    store,
    query: str,
    k: int = 20,
    project: str | None = None,
    explain: bool = False,
    detail: str = "full",
    include_context: bool | None = None,
    include_snippets: bool | None = None,
    pool_size: int | None = None,
) -> list[dict] | dict:
    overlay_store = getattr(store, "overlay_store", None)
    _pool = pool_size or SETTINGS.semantic_candidate_pool

    # ── Phase 1a: Lightweight symbol load (no embeddings) ────────────────
    # Embeddings are multi-KB each (384 floats).  Loading them for every
    # search is wasteful because `rank_semantic_sql` handles vector distance
    # natively in DuckDB.  Only load embeddings on-demand when the SQL
    # vector path is unavailable (fallback to Python cosine-similarity).
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
    detail = (detail or "full").lower().strip()
    if detail not in {"full", "compact"}:
        raise ValueError("detail must be 'full' or 'compact'")
    include_context = detail != "compact" if include_context is None else include_context
    include_snippets = detail != "compact" if include_snippets is None else include_snippets

    trace_limit = max(k, 1)
    exact_matches = [rec for rec in recs if _is_exact_query_match(query_lower, rec)]
    if exact_matches:
        exact_matches.sort(key=lambda rec: _exact_match_sort_key(query_lower, rec))

    bm25_rank: list[tuple[str, float]] = []
    fuzzy_rank: list[tuple[str, float]] = []
    semantic_rank: list[tuple[str, float]] = []
    if exact_matches:
        ranked = [(rec["id"], 1.0) for rec in exact_matches]
        bm25_trace_by_id: dict[str, dict[str, object]] = {}
        fuzzy_trace_by_id: dict[str, dict[str, object]] = {}
        semantic_trace_by_id: dict[str, dict[str, object]] = {}
        bm25_traces: list[dict[str, object]] = []
        fuzzy_traces: list[dict[str, object]] = []
        semantic_traces: list[dict[str, object]] = []
    else:
        # ── Phase 1b: Candidate pool pre-filter ─────────────────────────
        # For large symbol indexes, pre-filter the candidate pool using SQL
        # CONTAINS / LIKE before running expensive in-Python BM25/fuzzy.
        # If the query looks like a symbol name (no spaces, PascalCase), the
        # pre-filter is a safe precision gain.  For free-form questions
        # ("how does payment work?") we fall back to the full pool.
        _prefiltered = recs
        if len(recs) > _pool and not exact_matches:
            _query_words = query_lower.split()
            if len(_query_words) <= 3:
                # Short query → likely a symbol name → safe to pre-filter.
                try:
                    _prefiltered = _sql_prefilter_candidates(
                        store, query_lower, _pool, project=project,
                    )
                except Exception:
                    pass

        recs_by_id = {r["id"]: r for r in recs}
        lexical_docs = [(_r["id"], _build_lexical_text(_r)) for _r in _prefiltered]
        fuzzy_docs = [(_r["id"], _r.get("name", "")) for _r in _prefiltered]

        bm25_rank = rank_bm25(query, lexical_docs)
        fuzzy_rank = rank_fuzzy(query, fuzzy_docs)

        # ── Phase 1c: Semantic ranking via SQL (no Python embedding load) ──
        _sql_rank = rank_semantic_sql(store, query, pool_size=_pool)
        if _sql_rank is not None:
            semantic_rank = _sql_rank
        else:
            # On-demand embedding load — only happens when the SQL vector
            # path is unsupported (e.g. Kuzu backend, older DuckDB).
            emb_by_id: dict[str, list[float] | None] = {}
            try:
                emb_rows = store.query_records(
                    "MATCH (s:Symbol) WHERE s.embedding IS NOT NULL RETURN s.id as id, s.embedding as emb",
                    {},
                )
                if emb_rows:
                    # Handle both alias conventions: "emb" (DuckDB) vs "embedding" (mock stores).
                    for r in emb_rows:
                        eid = r.get("id")
                        emb = r.get("emb") or r.get("embedding")
                        if eid and emb is not None:
                            emb_by_id[eid] = emb
            except Exception:
                pass
            # Fallback: check if embeddings were pre-loaded in recs (legacy path).
            if not emb_by_id:
                for r in recs:
                    emb = r.get("embedding")
                    if emb is not None:
                        emb_by_id[r["id"]] = emb
            if emb_by_id:
                vector_docs = [(r["id"], emb_by_id.get(r["id"])) for r in recs_by_id.values()]
                semantic_rank = rank_semantic(query, vector_docs)
            else:
                LOGGER.info("Embeddings unavailable — skipping semantic ranking (BM25+fuzzy only)")

        bm25_trace_by_id, bm25_traces = _rank_trace_map(bm25_rank, trace_limit)
        fuzzy_trace_by_id, fuzzy_traces = _rank_trace_map(fuzzy_rank, trace_limit)
        semantic_trace_by_id, semantic_traces = _rank_trace_map(semantic_rank, trace_limit)

    dirty_overlay_paths: set[str] = set()
    if overlay_store is not None:
        try:
            dirty_overlay_paths = _dirty_overlay_file_paths(overlay_store, project)
        except Exception:
            dirty_overlay_paths = set()

    # Model-aware RRF: when a real sentence-transformer model is installed,
    # boost the semantic ranker weight.  With hash-based fallback, keep
    # BM25-primary fusion (equal weights) since hash vectors are less precise.
    _has_real_model = _load_model() is not None
    _rrf_pool = [bm25_rank, semantic_rank, fuzzy_rank]
    _rrf_weights = None
    if _has_real_model:
        # Real model: semantic (1.0), BM25 (0.8), fuzzy (0.6)
        _rrf_weights = [0.8, 1.0, 0.6]
    fused = ranked if exact_matches else reciprocal_rank_fusion(_rrf_pool, weights=_rrf_weights)

    # Cross-encoder reranking (optional, opt-in via config): apply to top N
    # candidates from the RRF pool for more precise ordering.
    _cross_encoder_model = SETTINGS.cross_encoder_model
    if _cross_encoder_model and not exact_matches:
        _top_n = fused[:_CROSS_ENCODER_RERANK_TOP_N]
        _rest = fused[_CROSS_ENCODER_RERANK_TOP_N:]
        if _top_n:
            ce_candidates = [
                (doc_id, _build_lexical_text(rec_by_id.get(doc_id, {})))
                for doc_id, _ in _top_n
                if doc_id in rec_by_id
            ]
            ce_ranked = rank_cross_encoder(query, ce_candidates)
            # Merge: cross-encoder results first, then remaining RRF results
            seen_ce = {doc_id for doc_id, _ in ce_ranked}
            fused = ce_ranked + [(doc_id, score) for doc_id, score in _rest if doc_id not in seen_ce]

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

        # Exact matches are sorted deterministically above when the fast path is used.
        if _is_exact_query_match(query_lower, rec):
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

    if not exact_matches:
        results.sort(key=lambda x: x["score"], reverse=True)
    top_k = results[:k]

    for rank, item in enumerate(top_k, start=1):
        item["rank"] = rank

    if include_context:
        # Attach architectural context in the same response. This is best-effort:
        # a context query failure must not hide ranked symbol results.
        context_by_id: dict[str, list[dict[str, object]]] | None = None
        try:
            context_by_id = _load_symbol_contexts(store, [item["id"] for item in top_k])
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Unable to batch load architectural context: %s", exc)

        for item in top_k:
            if item.get("file_path") and item["file_path"] in dirty_overlay_paths:
                item["context"] = []
                item["context_warning"] = "Architectural context unavailable for this result."
                item["context_source"] = "overlay_dirty"
                continue
            if context_by_id is not None:
                item["context"] = context_by_id.get(item["id"], [])
                continue
            try:
                item["context"] = _load_symbol_context(store, item["id"])
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Unable to load architectural context for %s: %s", item.get("id"), exc)
                item["context"] = []
                item["context_warning"] = "Architectural context unavailable for this result."

    if include_snippets:
        # Attach source code snippets (3–5 lines around the declaration) to the
        # top results so agents have immediate context without reading the file.
        for item in top_k:
            if isinstance(item, dict) and item.get("file_path") and item.get("line"):
                snippet = _read_snippet(item["file_path"], int(item["line"]))
                if snippet:
                    item["snippet"] = snippet

    # FR-10: Calibrate confidence labels based on NAME MATCHING (not score).
    # RRF scores (`1 / (rank + 60)`) max out at ~0.02–0.05, far below the 0.3
    # threshold that embedding cosine-similarity would produce.  Using absolute
    # score thresholds always gives "low".  Using rank position alone gives
    # false "medium" for single-doc edge cases.
    #
    # This heuristic uses CONTENT-BASED signals (name overlap, token match):
    #
    #   exact name/id/fqcn match           → "high"
    #   substring match on name/fqname     → "medium"
    #   query token in name/fqname         → "medium"
    #   no overlap at all                  → "low"
    #
    has_medium_or_high = False
    query_tokens = set(query_lower.split()) - set(_STOPWORDS)
    for item in top_k:
        if not isinstance(item, dict) or "score" not in item:
            continue
        item_name = (item.get("name") or "").lower()
        item_fqname = (item.get("fqname") or "").lower()
        if (item.get("id") or "").lower() == query_lower or item_name == query_lower or item_fqname == query_lower:
            item["confidence"] = "high"
            has_medium_or_high = True
        elif query_lower in item_name or query_lower in item_fqname:
            item["confidence"] = "medium"
            has_medium_or_high = True
        elif any(t in item_name or t in item_fqname for t in query_tokens):
            item["confidence"] = "medium"
            has_medium_or_high = True
        else:
            item["confidence"] = "low"

    low_confidence_note: str | None = None

    # Only warn when ALL results are "low" — signals a genuine miss.
    if not has_medium_or_high and top_k:
        has_model = _load_model() is not None
        for item in top_k:
            if isinstance(item, dict) and "score" in item:
                item["low_confidence"] = True
        if has_model:
            low_confidence_note = (
                "Low confidence results — no exact/substring/token overlap. "
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
        "retrieval_contract": {
            "version": _SEARCH_PROVENANCE_VERSION,
            "fusion": "rrf",
            "rankers": ["bm25", "semantic", "fuzzy"],
            "candidate_pool_size": len(recs),
            "returned": len(top_k),
            "supports_rerank": True,
        },
        "provenance": {
            "version": _SEARCH_PROVENANCE_VERSION,
            "package_version": __version__,
            "candidate_pool_size": len(recs),
            "index_fingerprint": {
                "snapshot_mtime": _store_snapshot_mtime(store, project),
                "overlay_mtime": _overlay_snapshot_mtime(store, project),
            },
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
