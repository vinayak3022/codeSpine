from __future__ import annotations

from codespine.analysis.community import symbol_community
from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact
from codespine.search.hybrid import hybrid_search


def _focus_symbol(focus: dict[str, object] | None, fallback: str) -> str:
    if not focus:
        return fallback
    return str(focus.get("fqname") or focus.get("name") or focus.get("id") or fallback)


def build_symbol_context(store, query: str, max_depth: int = 3, project: str | None = None) -> dict:
    search_results = hybrid_search(store, query, k=10, project=project)
    focus = search_results[0] if search_results else None
    focus_symbol = _focus_symbol(focus, query)

    impact = analyze_impact(store, focus_symbol, max_depth=max_depth, project=project)
    community = symbol_community(store, focus_symbol, project=project)
    flows = trace_execution_flows(store, entry_symbol=focus_symbol, max_depth=max_depth + 2, project=project)

    return {
        "query": query,
        "focus": focus,
        "search_candidates": search_results,
        "impact": impact,
        "community": community,
        "flows": flows,
    }
