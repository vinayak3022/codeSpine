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


def resolve_symbol_focus(
    store,
    query: str,
    *,
    project: str | None = None,
    detail: str = "full",
    k: int = 10,
    search_candidates: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    started = time.perf_counter()
    candidates = search_candidates if search_candidates is not None else hybrid_search(store, query, k=k, project=project, detail=detail)
    search_ms = int((time.perf_counter() - started) * 1000)
    if search_candidates is not None:
        search_ms = max(1, search_ms)
    focus = candidates[0] if candidates else None
    focus_symbol = _focus_symbol(focus, query)
    return {
        "query": query,
        "focus": focus,
        "focus_symbol": focus_symbol,
        "search_candidates": candidates,
        "search_ms": search_ms,
    }


def build_symbol_context(
    store,
    query: str,
    max_depth: int = 3,
    project: str | None = None,
    detail: str = "full",
    focus_resolution: dict[str, object] | None = None,
) -> dict:
    started = time.perf_counter()
    resolution = focus_resolution or resolve_symbol_focus(store, query, project=project, detail=detail, k=10)
    search_results = list(resolution.get("search_candidates") or [])
    search_ms = int(resolution.get("search_ms") or 0)
    if search_results and search_ms == 0:
        search_ms = 1
    focus = resolution.get("focus")
    focus_symbol = str(resolution.get("focus_symbol") or _focus_symbol(focus if isinstance(focus, dict) else None, query))

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
