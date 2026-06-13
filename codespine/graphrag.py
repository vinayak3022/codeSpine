from __future__ import annotations

import time
from difflib import SequenceMatcher

from codespine.analysis.context import build_symbol_context

_OBSERVABILITY_PRIMITIVES = [
    "hybrid_search",
    "build_symbol_context",
    "analyze_impact",
    "symbol_community",
    "trace_execution_flows",
]

_EVIDENCE_KIND_WEIGHTS = {
    "impact": 3.0,
    "search_result": 2.7,
    "community": 2.2,
    "flow": 2.0,
}


def _pretty_evidence_kind(kind: str) -> str:
    return {"search_result": "search", "impact": "impact", "community": "community", "flow": "flow"}.get(kind, kind)


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _candidate_snapshot(candidate: dict[str, object], *, rank: int | None = None) -> dict[str, object]:
    snapshot = {
        "id": candidate.get("id"),
        "name": candidate.get("name"),
        "fqname": candidate.get("fqname"),
        "file_path": candidate.get("file_path"),
        "line": candidate.get("line"),
        "score": candidate.get("score"),
        "confidence": candidate.get("confidence"),
    }
    if rank is not None:
        snapshot["rank"] = rank
    return snapshot


def _symbol_variants(candidate: dict[str, object]) -> list[str]:
    variants: list[str] = []
    for field in ("name", "fqname", "id"):
        value = str(candidate.get(field) or "").strip().lower()
        if not value:
            continue
        variants.append(value)
        leaf = value.rsplit("#", 1)[-1].rsplit(".", 1)[-1].split("(", 1)[0]
        if leaf and leaf not in variants:
            variants.append(leaf)
    return _unique_preserve_order(variants)


def _detect_ambiguity(search_candidates: list[dict[str, object]]) -> dict[str, object] | None:
    if len(search_candidates) < 2:
        return None

    top = search_candidates[0]
    top_variants = _symbol_variants(top)
    if not top_variants:
        return None

    top_score = float(top.get("score") or 0.0)
    score_window = max(0.03, abs(top_score) * 0.05)

    alternatives: list[dict[str, object]] = []
    for rank, candidate in enumerate(search_candidates[1:5], start=2):
        cand_variants = _symbol_variants(candidate)
        if not cand_variants:
            continue

        cand_score = float(candidate.get("score") or 0.0)
        same_symbol = any(left == right for left in top_variants for right in cand_variants)
        if same_symbol:
            alternatives.append(_candidate_snapshot(candidate, rank=rank))
            continue

        near_tie = cand_score >= top_score - score_window
        if not near_tie:
            continue

        overlaps = any(left in right or right in left for left in top_variants for right in cand_variants)
        similarity = max(SequenceMatcher(None, left, right).ratio() for left in top_variants for right in cand_variants)
        if overlaps or similarity >= 0.8:
            alternatives.append(_candidate_snapshot(candidate, rank=rank))

    if not alternatives:
        return None

    return {
        "status": "ambiguous",
        "reason": "Multiple symbols match this query closely enough that guessing would be unsafe.",
        "primary": _candidate_snapshot(top, rank=1),
        "alternatives": alternatives,
        "recommended_action": "Use find_symbol(name, project=..., limit=...) or qualify the package/class name.",
    }


def _abstain_response(
    question: str,
    *,
    project: str | None,
    max_depth: int,
    k: int,
    elapsed_ms: int,
    search_candidates: list[dict[str, object]],
    candidate_counts: dict[str, int],
    note: str,
    ambiguity: dict[str, object] | None = None,
    context_note: str | None = None,
) -> dict[str, object]:
    response: dict[str, object] = {
        "available": False,
        "abstained": True,
        "question": question,
        "answer": "",
        "focus": {},
        "confidence": {
            "label": "low",
            "score": 0.0,
            "reason": note,
            "evidence_count": 0,
            "evidence_kinds": [],
            "supporting_signals": [],
        },
        "evidence": [],
        "citations": [],
        "evidence_subgraph": {"nodes": [], "edges": []},
        "note": note,
        "fallback": {
            "recommended_tools": ["find_symbol", "search_hybrid"],
            "reason": "The answer surface refused to guess without a unique, grounded symbol match.",
        },
        "answer_contract": {
            "status": "abstained",
            "grounded": False,
            "requires_citations": True,
            "fallback_mode": True,
            "ambiguity": ambiguity,
            "supported_by": [],
        },
        "supporting_context": {
            "impact_summary": {},
            "community": None,
            "flow_count": 0,
            "search_candidate_count": len(search_candidates),
            "community_label": None,
            "evidence_kinds": [],
            "evidence_sources": [],
            "evidence_subgraph_nodes": 0,
            "evidence_subgraph_edges": 0,
            "context_note": context_note,
        },
        "observability": {
            "retrieval_mode": "graph_rag",
            "primitives": _OBSERVABILITY_PRIMITIVES,
            "elapsed_ms": elapsed_ms,
            "project": project,
            "max_depth": max_depth,
            "k": k,
            "search_candidates": len(search_candidates),
            "evidence_count": 0,
            "citation_count": 0,
            "evidence_rerank": {"strategy": "utility_ranked", "candidate_counts": candidate_counts, "selected": []},
        },
    }
    if ambiguity:
        response["ambiguity"] = ambiguity
    if context_note:
        response["context_note"] = context_note
    return response


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


def _confidence_payload(
    focus: dict[str, object], evidence: list[dict[str, object]], impact_summary: dict[str, object]
) -> dict[str, object]:
    evidence_count = len(evidence)
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
    evidence_kinds = [str(item.get("kind") or "evidence") for item in evidence]
    evidence_sources = _unique_preserve_order([str(item.get("source") or item.get("kind") or "evidence") for item in evidence])
    reason = focus.get("confidence_reason")
    if not reason:
        if evidence_count == 0:
            reason = "No supporting evidence selected; confidence comes from the focus match alone."
        else:
            pretty_kinds = _unique_preserve_order([_pretty_evidence_kind(kind) for kind in evidence_kinds])
            reason = f"Combined graph evidence from {', '.join(pretty_kinds)} via {', '.join(evidence_sources)}."
    return {
        "label": label,
        "score": round(score, 3),
        "reason": reason,
        "evidence_count": evidence_count,
        "evidence_kinds": evidence_kinds,
        "supporting_signals": evidence_sources,
    }


def _summarize_answer(
    focus: dict[str, object], impact_summary: dict[str, object], community: dict | None, flows: list[dict], evidence: list[dict[str, object]]
) -> str:
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
    evidence_kinds = sorted({_pretty_evidence_kind(str(item.get("kind") or "evidence")) for item in evidence})
    if evidence_kinds:
        parts.append(f"Evidence: {', '.join(evidence_kinds)}.")
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


def _evidence_node(kind: str, entry: dict[str, object]) -> dict[str, object]:
    node_id = entry.get("symbol_id") or entry.get("community_id") or entry.get("flow_id") or entry.get("id")
    if not node_id:
        node_id = entry.get("title") or kind
    return {
        "id": str(node_id),
        "symbol_id": str(entry.get("symbol_id") or node_id),
        "kind": kind,
        "label": str(entry.get("title") or entry.get("name") or entry.get("fqname") or entry.get("flow_kind") or kind),
        "file_path": entry.get("file_path"),
        "line": entry.get("line"),
    }


def _unique_subgraph_id(base_id: str, existing_ids: set[str], *, hint: str) -> str:
    if base_id not in existing_ids:
        return base_id
    suffix = hint or "evidence"
    candidate = f"{base_id}::{suffix}"
    counter = 2
    while candidate in existing_ids:
        candidate = f"{base_id}::{suffix}:{counter}"
        counter += 1
    return candidate


def _build_evidence_subgraph(focus: dict[str, object], evidence: list[dict[str, object]]) -> dict[str, object]:
    focus_id = str(focus.get("id") or focus.get("fqname") or focus.get("name") or "focus")
    nodes: dict[str, dict[str, object]] = {
        focus_id: {
            "id": focus_id,
            "kind": str(focus.get("kind") or "symbol"),
            "label": str(focus.get("fqname") or focus.get("name") or focus_id),
            "file_path": focus.get("file_path"),
            "line": focus.get("line"),
            "role": "focus",
        }
    }
    edges: list[dict[str, object]] = []

    for entry in evidence:
        node = _evidence_node(str(entry.get("kind") or "evidence"), entry)
        node_id = _unique_subgraph_id(
            str(node["id"]),
            set(nodes),
            hint=str(entry.get("citation_id") or entry.get("kind") or "evidence"),
        )
        if node_id != node["id"]:
            node = {**node, "id": node_id}
        nodes[node_id] = node
        edges.append(
            {
                "source": focus_id,
                "target": node_id,
                "kind": str(entry.get("kind") or "evidence"),
                "source_label": str(focus.get("fqname") or focus.get("name") or focus_id),
                "target_label": node["label"],
                "citation_id": entry.get("citation_id"),
            }
        )

        subgraph = entry.get("subgraph")
        if isinstance(subgraph, dict):
            subgraph_id_map: dict[str, str] = {}
            for sub_node in subgraph.get("nodes") or []:
                if isinstance(sub_node, dict) and sub_node.get("id") is not None:
                    original_id = str(sub_node["id"])
                    if original_id == str(node.get("symbol_id") or ""):
                        subgraph_id_map[original_id] = node_id
                        continue
                    subgraph_id = _unique_subgraph_id(
                        original_id,
                        set(nodes) | set(subgraph_id_map.values()),
                        hint=str(entry.get("citation_id") or sub_node.get("role") or sub_node.get("kind") or "subgraph"),
                    )
                    subgraph_id_map[original_id] = subgraph_id
                    nodes[subgraph_id] = {**sub_node, "id": subgraph_id, **({"symbol_id": original_id} if subgraph_id != original_id else {})}
            for sub_edge in subgraph.get("edges") or []:
                if isinstance(sub_edge, dict):
                    edge = dict(sub_edge)
                    if edge.get("source") in subgraph_id_map and str(edge.get("source")) != focus_id:
                        edge["source"] = subgraph_id_map[str(edge["source"])]
                    if edge.get("target") in subgraph_id_map:
                        edge["target"] = subgraph_id_map[str(edge["target"])]
                    edges.append(edge)

    return {"nodes": list(nodes.values()), "edges": edges}


def _build_evidence(
    focus: dict[str, object],
    search_candidates: list[dict[str, object]],
    impact: dict[str, object],
    community: dict | None,
    flows: list[dict],
    max_evidence: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    limit = max(0, int(max_evidence))
    if limit <= 0:
        return [], []

    candidates: list[dict[str, object]] = []
    candidate_counts = {"search_result": 0, "impact": 0, "community": 0, "flow": 0}
    citation_counter = 0

    def add_candidate(entry: dict[str, object]) -> None:
        candidates.append(entry)
        kind = str(entry.get("kind") or "")
        if kind in candidate_counts:
            candidate_counts[kind] += 1

    def make_citation(**kwargs: object) -> tuple[str, dict[str, object]]:
        nonlocal citation_counter
        citation_counter += 1
        citation_id = f"c{citation_counter}"
        citation: dict[str, object] = {"id": citation_id, "kind": str(kwargs["kind"]), "title": str(kwargs["title"]), "source": str(kwargs["source"])}
        for field in ("file_path", "line", "symbol_id", "community_id", "flow_id"):
            value = kwargs.get(field)
            if value is not None:
                citation[field] = value
        return citation_id, citation

    def _search_candidate_score(candidate: dict[str, object]) -> float:
        score = _EVIDENCE_KIND_WEIGHTS["search_result"]
        candidate_score = candidate.get("score")
        if isinstance(candidate_score, (int, float)):
            score += min(max(float(candidate_score), 0.0), 1.0) * 0.15
        confidence = str(candidate.get("confidence") or "low")
        score += {"high": 0.25, "medium": 0.12, "low": 0.0}.get(confidence, 0.0)
        exact_anchor = False
        if str(candidate.get("symbol_id") or "") == str(focus.get("id") or ""):
            score += 0.35
            exact_anchor = True
        if str(candidate.get("fqname") or "") == str(focus.get("fqname") or ""):
            score += 0.2
            exact_anchor = True
        if exact_anchor:
            score += 1.0
        return score

    def _impact_candidate_score(candidate: dict[str, object]) -> float:
        score = _EVIDENCE_KIND_WEIGHTS["impact"]
        depth = candidate.get("depth")
        if depth == 1:
            score += 0.35
        elif depth == 2:
            score += 0.2
        elif depth:
            score += 0.1
        confidence = candidate.get("confidence")
        if isinstance(confidence, (int, float)):
            score += min(max(float(confidence), 0.0), 1.0) * 0.2
        return score

    def _community_candidate_score(candidate: dict[str, object]) -> float:
        score = _EVIDENCE_KIND_WEIGHTS["community"]
        cohesion = candidate.get("cohesion")
        if isinstance(cohesion, (int, float)):
            score += min(max(float(cohesion), 0.0), 1.0) * 0.25
        return score

    def _flow_candidate_score(candidate: dict[str, object]) -> float:
        score = _EVIDENCE_KIND_WEIGHTS["flow"]
        depth = candidate.get("flow_depth")
        if depth == 0:
            score += 0.25
        elif isinstance(depth, int) and depth > 0:
            score += max(0.05, 0.2 - depth * 0.03)
        return score

    for candidate in search_candidates:
        title = str(candidate.get("fqname") or candidate.get("name") or candidate.get("id") or "search result")
        citation_id, citation = make_citation(
            kind="symbol",
            title=title,
            source="hybrid_search",
            file_path=candidate.get("file_path"),
            line=candidate.get("line"),
            symbol_id=str(candidate.get("id") or "") or None,
        )
        add_candidate(
            {
                "kind": "search_result",
                "citation_id": citation_id,
                "citation": citation,
                "source": "hybrid_search",
                "symbol_id": candidate.get("id"),
                "title": title,
                "file_path": candidate.get("file_path"),
                "line": candidate.get("line"),
                "confidence": candidate.get("confidence"),
                "score": candidate.get("score"),
                "snippet": candidate.get("snippet"),
                "is_focus_anchor": str(candidate.get("id") or "") == str(focus.get("id") or "")
                or str(candidate.get("fqname") or "") == str(focus.get("fqname") or ""),
                "subgraph": {
                    "nodes": [
                        {
                            "id": str(candidate.get("id") or title),
                            "kind": "symbol",
                            "label": title,
                            "file_path": candidate.get("file_path"),
                            "line": candidate.get("line"),
                            "role": "search_result",
                        }
                    ],
                    "edges": [
                        {
                            "source": str(focus.get("id") or focus.get("fqname") or focus.get("name") or "focus"),
                            "target": str(candidate.get("id") or title),
                            "kind": "search_result",
                            "citation_id": citation_id,
                        }
                    ],
                },
                "rerank_score": _search_candidate_score(candidate),
            }
        )

    impact_groups = impact.get("impacted_callers") or {}
    for depth_key in ("1", "2", "3+"):
        for caller in (impact_groups.get(depth_key) or [])[:2]:
            title = str(caller.get("fqname") or caller.get("name") or caller.get("symbol") or "impact caller")
            citation_id, citation = make_citation(
                kind="method",
                title=title,
                source="analyze_impact",
                file_path=caller.get("file_path"),
                line=caller.get("line"),
                symbol_id=str(caller.get("symbol") or "") or None,
            )
            add_candidate(
                {
                    "kind": "impact",
                    "citation_id": citation_id,
                    "citation": citation,
                    "source": "analyze_impact",
                    "symbol_id": caller.get("symbol"),
                    "title": title,
                    "depth": caller.get("depth"),
                    "edge_type": caller.get("edge_type"),
                    "confidence": caller.get("confidence"),
                    "file_path": caller.get("file_path"),
                    "path": caller.get("path"),
                    "subgraph": {
                        "nodes": [
                            {
                                "id": str(caller.get("symbol") or title),
                                "kind": "symbol",
                                "label": title,
                                "file_path": caller.get("file_path"),
                                "line": caller.get("line"),
                                "role": "impact",
                                "depth": caller.get("depth"),
                            }
                        ],
                        "edges": [
                            {
                                "source": str(caller.get("symbol") or title),
                                "target": str(focus.get("id") or focus.get("fqname") or focus.get("name") or "focus"),
                                "kind": str(caller.get("edge_type") or "impact"),
                                "citation_id": citation_id,
                            }
                        ],
                    },
                    "rerank_score": _impact_candidate_score(caller),
                }
            )

    for community_match in _community_matches(community)[:2]:
        title = str(community_match.get("community_label") or community_match.get("label") or community_match.get("community_id") or "community")
        citation_id, citation = make_citation(
            kind="community",
            title=title,
            source="symbol_community",
            symbol_id=str(focus.get("id") or "") or None,
            community_id=str(community_match.get("community_id") or "") or None,
        )
        add_candidate(
            {
                "kind": "community",
                "citation_id": citation_id,
                "citation": citation,
                "source": "symbol_community",
                "title": title,
                "community_id": community_match.get("community_id"),
                "community_label": community_match.get("community_label") or community_match.get("label"),
                "cohesion": community_match.get("cohesion"),
                "subgraph": {
                    "nodes": [
                        {
                            "id": str(community_match.get("community_id") or title),
                            "kind": "community",
                            "label": title,
                            "role": "community",
                            "cohesion": community_match.get("cohesion"),
                        }
                    ],
                    "edges": [
                        {
                            "source": str(focus.get("id") or focus.get("fqname") or focus.get("name") or "focus"),
                            "target": str(community_match.get("community_id") or title),
                            "kind": "community_membership",
                            "citation_id": citation_id,
                        }
                    ],
                },
                "rerank_score": _community_candidate_score(community_match),
            }
        )

    for flow in flows[:2]:
        title = str(flow.get("flow_kind") or flow.get("flow_id") or "flow")
        citation_id, citation = make_citation(
            kind="flow",
            title=title,
            source="trace_execution_flows",
            flow_id=str(flow.get("flow_id") or "") or None,
        )
        add_candidate(
            {
                "kind": "flow",
                "citation_id": citation_id,
                "citation": citation,
                "source": "trace_execution_flows",
                "title": title,
                "flow_id": flow.get("flow_id"),
                "flow_kind": flow.get("flow_kind"),
                "flow_depth": flow.get("flow_depth"),
                "subgraph": {
                    "nodes": [
                        {
                            "id": str(flow.get("flow_id") or title),
                            "kind": "flow",
                            "label": title,
                            "role": "flow",
                            "flow_depth": flow.get("flow_depth"),
                        }
                    ],
                    "edges": [
                        {
                            "source": str(focus.get("id") or focus.get("fqname") or focus.get("name") or "focus"),
                            "target": str(flow.get("flow_id") or title),
                            "kind": "execution_flow",
                            "citation_id": citation_id,
                        }
                    ],
                },
                "rerank_score": _flow_candidate_score(flow),
            }
        )

    selected_candidates = sorted(
        candidates,
        key=lambda item: (
            float(item.get("rerank_score") or 0.0),
            item.get("kind") == "impact",
            item.get("kind") == "search_result",
            str(item.get("title") or "").lower(),
            str(item.get("citation_id") or ""),
        ),
        reverse=True,
    )[:limit]

    evidence: list[dict[str, object]] = []
    citations: list[dict[str, object]] = []
    for index, candidate in enumerate(selected_candidates, start=1):
        evidence_item = {k: v for k, v in candidate.items() if k != "citation"}
        evidence_item["id"] = f"e{index}"
        evidence.append(evidence_item)
        citations.append(candidate["citation"])

    return evidence, citations


def graph_rag_answer(store, question: str, *, project: str | None = None, max_depth: int = 3, k: int = 5) -> dict:
    started = time.perf_counter()
    context = build_symbol_context(store, question, max_depth=max_depth, project=project)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    evidence_limit = max(0, int(k))

    focus = context.get("focus") or {}
    search_candidates = list(context.get("search_candidates") or [])
    impact = context.get("impact") or {}
    community = context.get("community")
    flows = [_normalize_flow(flow) for flow in (context.get("flows") or [])]
    context_note = context.get("note")

    impact_groups = impact.get("impacted_callers") or {}
    candidate_counts = {
        "search_result": len(search_candidates),
        "impact": sum(len((impact_groups.get(depth_key) or [])[:2]) for depth_key in ("1", "2", "3+")),
        "community": len(_community_matches(community)[:2]),
        "flow": len(flows[:2]),
    }

    ambiguity = _detect_ambiguity(search_candidates)
    if ambiguity:
        return _abstain_response(
            question,
            project=project,
            max_depth=max_depth,
            k=evidence_limit,
            elapsed_ms=elapsed_ms,
            search_candidates=search_candidates,
            candidate_counts=candidate_counts,
            note=f"Ambiguous symbol resolution for GraphRAG answer: {ambiguity['reason']}",
            ambiguity=ambiguity,
            context_note=str(context_note) if context_note else None,
        )

    if not focus:
        return _abstain_response(
            question,
            project=project,
            max_depth=max_depth,
            k=evidence_limit,
            elapsed_ms=elapsed_ms,
            search_candidates=search_candidates,
            candidate_counts=candidate_counts,
            note="No symbol match found for a GraphRAG answer.",
            context_note=str(context_note) if context_note else None,
        )

    impact_summary = impact.get("summary") or {}
    evidence, citations = _build_evidence(focus, search_candidates, impact, community, flows, evidence_limit)
    if not evidence:
        return _abstain_response(
            question,
            project=project,
            max_depth=max_depth,
            k=evidence_limit,
            elapsed_ms=elapsed_ms,
            search_candidates=search_candidates,
            candidate_counts=candidate_counts,
            note="GraphRAG could not assemble enough grounded evidence to answer safely.",
            context_note=str(context_note) if context_note else None,
        )

    evidence_subgraph = _build_evidence_subgraph(focus, evidence)
    confidence = _confidence_payload(focus, evidence, impact_summary)

    return {
        "available": True,
        "abstained": False,
        "question": question,
        "focus": focus,
        "answer": _summarize_answer(focus, impact_summary, community, flows, evidence),
        "confidence": confidence,
        "evidence": evidence,
        "citations": citations,
        "evidence_subgraph": evidence_subgraph,
        "answer_contract": {
            "status": "supported",
            "grounded": True,
            "requires_citations": True,
            "fallback_mode": False,
            "ambiguity": None,
            "supported_by": _unique_preserve_order([str(item.get("source") or item.get("kind") or "evidence") for item in evidence]),
        },
        "supporting_context": {
            "impact_summary": impact_summary,
            "community": community,
            "flow_count": len(flows),
            "search_candidate_count": len(search_candidates),
            "community_label": _community_label(community),
            "evidence_kinds": [str(item.get("kind") or "evidence") for item in evidence],
            "evidence_sources": sorted({str(item.get("source") or item.get("kind") or "evidence") for item in evidence}),
            "evidence_subgraph_nodes": len(evidence_subgraph["nodes"]),
            "evidence_subgraph_edges": len(evidence_subgraph["edges"]),
            "context_note": str(context_note) if context_note else None,
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
            "evidence_rerank": {
                "strategy": "utility_ranked",
                "candidate_counts": candidate_counts,
                "selected": [
                    {
                        "id": item["id"],
                        "kind": item.get("kind"),
                        "source": item.get("source"),
                        "title": item.get("title"),
                        "citation_id": item.get("citation_id"),
                        "confidence": item.get("confidence"),
                        "score": item.get("score"),
                        "rerank_score": item.get("rerank_score"),
                        "is_focus_anchor": item.get("is_focus_anchor"),
                    }
                    for item in evidence
                ],
            },
        },
    }
