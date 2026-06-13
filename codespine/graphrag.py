from __future__ import annotations

import time

from codespine.analysis.context import build_symbol_context

_OBSERVABILITY_PRIMITIVES = [
    "hybrid_search",
    "build_symbol_context",
    "analyze_impact",
    "symbol_community",
    "trace_execution_flows",
]


def _add_citation(
    citations: list[dict[str, object]],
    citation_index: dict[tuple[object, ...], str],
    *,
    kind: str,
    title: str,
    source: str,
    file_path: str | None = None,
    line: int | None = None,
    symbol_id: str | None = None,
    community_id: str | None = None,
    flow_id: str | None = None,
) -> str:
    key = (kind, title, source, file_path, line, symbol_id, community_id, flow_id)
    if key in citation_index:
        return citation_index[key]
    citation_id = f"c{len(citations) + 1}"
    citation: dict[str, object] = {"id": citation_id, "kind": kind, "title": title, "source": source}
    if file_path:
        citation["file_path"] = file_path
    if line is not None:
        citation["line"] = line
    if symbol_id:
        citation["symbol_id"] = symbol_id
    if community_id:
        citation["community_id"] = community_id
    if flow_id:
        citation["flow_id"] = flow_id
    citations.append(citation)
    citation_index[key] = citation_id
    return citation_id


def _confidence_payload(focus: dict[str, object], evidence_count: int, impact_summary: dict[str, object]) -> dict[str, object]:
    label = str(focus.get("confidence") or "low")
    base_scores = {"high": 0.92, "medium": 0.72, "low": 0.48}
    score = base_scores.get(label, 0.6)
    if evidence_count > 1:
        score += 0.04
    if impact_summary.get("direct") or impact_summary.get("indirect") or impact_summary.get("transitive"):
        score += 0.03
    score = min(score, 0.99)
    if score >= 0.85:
        label = "high"
    elif score >= 0.6:
        label = "medium"
    else:
        label = "low"
    reason = focus.get("confidence_reason") or "Combined graph evidence from search and impact analysis."
    if not focus.get("confidence_reason") and evidence_count > 1:
        reason = "Combined graph evidence from search, impact, and architectural context."
    return {"label": label, "score": round(score, 3), "reason": reason}


def _summarize_answer(focus: dict[str, object], impact_summary: dict[str, object], community: dict | None, flows: list[dict]) -> str:
    name = focus.get("fqname") or focus.get("name") or focus.get("id") or "the best-matching symbol"
    parts = [f"Best match: {name}."]
    direct = int(impact_summary.get("direct") or 0)
    indirect = int(impact_summary.get("indirect") or 0)
    transitive = int(impact_summary.get("transitive") or 0)
    if direct or indirect or transitive:
        parts.append(f"Impact: {direct} direct, {indirect} indirect, and {transitive} transitive callers.")
    if focus.get("file_path"):
        parts.append(f"Located in {focus['file_path']}.")
    community_label = _community_label(community)
    if community_label:
        parts.append(f"Community: {community_label}.")
    if flows:
        parts.append(f"Flow coverage: {len(flows)} path(s) found.")
    return " ".join(parts)


def _community_matches(community: dict | None) -> list[dict[str, object]]:
    if not community:
        return []
    matches = community.get("matches") if isinstance(community, dict) else None
    if isinstance(matches, list):
        return [m for m in matches if isinstance(m, dict)]
    return [community] if isinstance(community, dict) else []


def _community_label(community: dict | None) -> str | None:
    for rec in _community_matches(community):
        label = rec.get("community_label") or rec.get("label")
        if label:
            return str(label)
    if isinstance(community, dict):
        label = community.get("community_label") or community.get("label")
        if label:
            return str(label)
    return None


def _normalize_flow(flow: dict[str, object]) -> dict[str, object]:
    if not isinstance(flow, dict) or "nodes" not in flow:
        return flow

    nodes = [node for node in flow.get("nodes") or [] if isinstance(node, dict)]
    flow_depth = flow.get("flow_depth")
    if flow_depth is None and nodes:
        depths = [node.get("depth") for node in nodes if isinstance(node.get("depth"), int)]
        if depths:
            flow_depth = min(depths)

    return {
        **flow,
        "flow_id": str(flow.get("flow_id") or flow.get("entry") or flow.get("entry_fqname") or flow.get("entry_name") or "flow"),
        "flow_kind": flow.get("flow_kind") or flow.get("kind"),
        "flow_depth": flow_depth,
    }


def _build_evidence(
    focus: dict[str, object],
    search_candidates: list[dict[str, object]],
    impact: dict[str, object],
    community: dict | None,
    flows: list[dict],
    max_evidence: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    evidence: list[dict[str, object]] = []
    citations: list[dict[str, object]] = []
    citation_index: dict[tuple[object, ...], str] = {}
    limit = max(0, int(max_evidence))

    def add_evidence(entry: dict[str, object]) -> bool:
        if len(evidence) >= limit:
            return False
        entry["id"] = f"e{len(evidence) + 1}"
        evidence.append(entry)
        return True

    if limit <= 0:
        return evidence, citations

    for candidate in search_candidates:
        if len(evidence) >= limit:
            break
        title = str(candidate.get("fqname") or candidate.get("name") or candidate.get("id") or "search result")
        citation_id = _add_citation(
            citations,
            citation_index,
            kind="symbol",
            title=title,
            source="hybrid_search",
            file_path=candidate.get("file_path"),
            line=candidate.get("line"),
            symbol_id=str(candidate.get("id") or "") or None,
        )
        add_evidence(
            {
                "kind": "search_result",
                "citation_id": citation_id,
                "source": "hybrid_search",
                "symbol_id": candidate.get("id"),
                "title": title,
                "file_path": candidate.get("file_path"),
                "line": candidate.get("line"),
                "confidence": candidate.get("confidence"),
                "score": candidate.get("score"),
                "snippet": candidate.get("snippet"),
            }
        )

    impact_groups = impact.get("impacted_callers") or {}
    for depth_key in ("1", "2", "3+"):
        if len(evidence) >= limit:
            break
        for caller in (impact_groups.get(depth_key) or [])[:2]:
            if len(evidence) >= limit:
                break
            title = str(caller.get("fqname") or caller.get("name") or caller.get("symbol") or "impact caller")
            citation_id = _add_citation(
                citations,
                citation_index,
                kind="method",
                title=title,
                source="analyze_impact",
                file_path=caller.get("file_path"),
                line=caller.get("line"),
                symbol_id=str(caller.get("symbol") or "") or None,
            )
            add_evidence(
                {
                    "kind": "impact",
                    "citation_id": citation_id,
                    "source": "analyze_impact",
                    "symbol_id": caller.get("symbol"),
                    "title": title,
                    "depth": caller.get("depth"),
                    "edge_type": caller.get("edge_type"),
                    "confidence": caller.get("confidence"),
                    "file_path": caller.get("file_path"),
                    "path": caller.get("path"),
                }
            )
    
    for community_match in _community_matches(community)[:2]:
        if len(evidence) >= limit:
            break
        title = str(community_match.get("community_label") or community_match.get("label") or community_match.get("community_id") or "community")
        citation_id = _add_citation(
            citations,
            citation_index,
            kind="community",
            title=title,
            source="symbol_community",
            symbol_id=str(focus.get("id") or "") or None,
            community_id=str(community_match.get("community_id") or "") or None,
        )
        add_evidence(
            {
                "kind": "community",
                "citation_id": citation_id,
                "source": "symbol_community",
                "title": title,
                "community_id": community_match.get("community_id"),
                "community_label": community_match.get("community_label") or community_match.get("label"),
                "cohesion": community_match.get("cohesion"),
            }
        )

    for flow in flows[:2]:
        if len(evidence) >= limit:
            break
        title = str(flow.get("flow_kind") or flow.get("flow_id") or "flow")
        citation_id = _add_citation(
            citations,
            citation_index,
            kind="flow",
            title=title,
            source="trace_execution_flows",
            flow_id=str(flow.get("flow_id") or "") or None,
        )
        add_evidence(
            {
                "kind": "flow",
                "citation_id": citation_id,
                "source": "trace_execution_flows",
                "title": title,
                "flow_id": flow.get("flow_id"),
                "flow_kind": flow.get("flow_kind"),
                "flow_depth": flow.get("flow_depth"),
            }
        )

    return evidence, citations


def graph_rag_answer(store, question: str, *, project: str | None = None, max_depth: int = 3, k: int = 5) -> dict:
    started = time.perf_counter()
    context = build_symbol_context(store, question, max_depth=max_depth, project=project)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    evidence_limit = max(0, int(k))

    focus = context.get("focus") or {}
    search_candidates = list(context.get("search_candidates") or [])[:evidence_limit]
    impact = context.get("impact") or {}
    community = context.get("community")
    flows = [_normalize_flow(flow) for flow in (context.get("flows") or [])]

    if not focus:
        return {
            "available": False,
            "question": question,
            "note": "No symbol match found for a GraphRAG answer.",
            "observability": {
                "retrieval_mode": "graph_rag",
                "primitives": _OBSERVABILITY_PRIMITIVES,
                "elapsed_ms": elapsed_ms,
                "project": project,
                "max_depth": max_depth,
                "k": evidence_limit,
                "search_candidates": len(search_candidates),
            },
        }

    impact_summary = impact.get("summary") or {}
    evidence, citations = _build_evidence(focus, search_candidates, impact, community, flows, evidence_limit)
    confidence = _confidence_payload(focus, len(evidence), impact_summary)

    return {
        "available": True,
        "question": question,
        "focus": focus,
        "answer": _summarize_answer(focus, impact_summary, community, flows),
        "confidence": confidence,
        "evidence": evidence,
        "citations": citations,
        "supporting_context": {
            "impact_summary": impact_summary,
            "community": community,
            "flow_count": len(flows),
            "search_candidate_count": len(search_candidates),
            "community_label": _community_label(community),
        },
        "observability": {
            "retrieval_mode": "graph_rag",
            "primitives": _OBSERVABILITY_PRIMITIVES,
            "elapsed_ms": elapsed_ms,
            "project": project,
            "max_depth": max_depth,
            "k": evidence_limit,
            "search_candidates": len(search_candidates),
            "evidence_count": len(evidence),
            "citation_count": len(citations),
        },
    }
