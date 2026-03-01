from __future__ import annotations

from fastmcp import FastMCP

from codespine.analysis.community import detect_communities, symbol_community
from codespine.analysis.coupling import get_coupling
from codespine.analysis.deadcode import detect_dead_code as detect_dead_code_analysis
from codespine.analysis.flow import trace_execution_flows as trace_flows_analysis
from codespine.analysis.impact import analyze_impact
from codespine.diff.branch_diff import compare_branches as compare_branches_analysis
from codespine.search.hybrid import hybrid_search


def build_mcp_server(store, repo_path_provider):
    mcp = FastMCP("codespine")

    @mcp.tool()
    def search_hybrid(query: str, k: int = 20):
        return hybrid_search(store, query, k=k)

    @mcp.tool()
    def get_impact(symbol: str, max_depth: int = 4):
        return analyze_impact(store, symbol, max_depth=max_depth)

    @mcp.tool()
    def detect_dead_code(limit: int = 200):
        return detect_dead_code_analysis(store, limit=limit)

    @mcp.tool()
    def trace_execution_flows(entry_symbol: str | None = None, max_depth: int = 6):
        flows = trace_flows_analysis(store, entry_symbol=entry_symbol, max_depth=max_depth)
        return flows

    @mcp.tool()
    def get_symbol_community(symbol: str):
        detect_communities(store)
        return symbol_community(store, symbol)

    @mcp.tool()
    def get_change_coupling(symbol: str | None = None, months: int = 6, min_strength: float = 0.3, min_cochanges: int = 3):
        return get_coupling(store, symbol=symbol, months=months, min_strength=min_strength, min_cochanges=min_cochanges)

    @mcp.tool()
    def compare_branches(base_ref: str, head_ref: str):
        return compare_branches_analysis(repo_path_provider(), base_ref, head_ref)

    @mcp.tool()
    def get_codebase_stats():
        projects = store.query_records("MATCH (p:Project) RETURN p.id as project, p.path as path")
        symbols = store.query_records("MATCH (s:Symbol) RETURN count(s) as count")
        calls = store.query_records("MATCH (:Method)-[r:CALLS]->(:Method) RETURN count(r) as count")
        return {
            "projects": projects,
            "symbols": symbols[0]["count"] if symbols else 0,
            "calls": calls[0]["count"] if calls else 0,
        }

    return mcp
