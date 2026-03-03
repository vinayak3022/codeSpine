from __future__ import annotations

from codespine.analysis.community import symbol_community
from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact
from codespine.search.hybrid import hybrid_search


def build_symbol_context(store, query: str, max_depth: int = 3, project: str | None = None) -> dict:
    search_results = hybrid_search(store, query, k=10, project=project)
    focus = search_results[0] if search_results else None

    impact = analyze_impact(store, query, max_depth=max_depth, project=project)
    community = symbol_community(store, query)
    flows = trace_execution_flows(store, entry_symbol=query, max_depth=max_depth + 2, project=project)

    return {
        "query": query,
        "focus": focus,
        "search_candidates": search_results,
        "impact": impact,
        "community": community,
        "flows": flows,
    }
