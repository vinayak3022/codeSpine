from __future__ import annotations

import time

from codespine.analysis.community import symbol_community
from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact
from codespine.search.hybrid import hybrid_search


def _focus_symbol(focus: dict[str, object] | None, fallback: str) -> str:
    if not focus:
        return fallback
    return str(focus.get("fqname") or focus.get("name") or focus.get("id") or fallback)


def build_symbol_context(store, query: str, max_depth: int = 3, project: str | None = None) -> dict:
    started = time.perf_counter()
    search_results = hybrid_search(store, query, k=10, project=project)
    search_ms = int((time.perf_counter() - started) * 1000)
    focus = search_results[0] if search_results else None
    focus_symbol = _focus_symbol(focus, query)

    if not focus:
        return {
            "query": query,
            "focus": None,
            "search_candidates": search_results,
            "impact": {"target": focus_symbol, "depth_groups": {"1": [], "2": [], "3+": []}},
            "community": {"query": focus_symbol, "matches": []},
            "flows": [],
            "timings_ms": {"search": search_ms, "impact": 0, "community": 0, "flows": 0, "total": search_ms},
            "note": "No usable focus symbol found; deep context omitted.",
        }

    if focus and focus.get("context_source") == "overlay_dirty":
        return {
            "query": query,
            "focus": focus,
            "search_candidates": search_results,
            "impact": {"target": focus_symbol, "depth_groups": {"1": [], "2": [], "3+": []}},
            "community": {"query": focus_symbol, "matches": []},
            "flows": [],
            "timings_ms": {"search": search_ms, "impact": 0, "community": 0, "flows": 0, "total": search_ms},
            "note": "Overlay-dirty focus symbol; architectural context omitted to avoid stale base-index data.",
        }

    impact_started = time.perf_counter()
    impact = analyze_impact(store, focus_symbol, max_depth=max_depth, project=project)
    impact_ms = int((time.perf_counter() - impact_started) * 1000)
    community_started = time.perf_counter()
    community = symbol_community(store, focus_symbol, project=project)
    community_ms = int((time.perf_counter() - community_started) * 1000)
    flows_started = time.perf_counter()
    flows = trace_execution_flows(store, entry_symbol=focus_symbol, max_depth=max_depth + 2, project=project)
    flows_ms = int((time.perf_counter() - flows_started) * 1000)

    return {
        "query": query,
        "focus": focus,
        "search_candidates": search_results,
        "impact": impact,
        "community": community,
        "flows": flows,
        "timings_ms": {
            "search": search_ms,
            "impact": impact_ms,
            "community": community_ms,
            "flows": flows_ms,
            "total": int((time.perf_counter() - started) * 1000),
        },
    }
