"""
Analysis tools for the CodeSpine MCP server.

Extracted from ``server.py`` to break up the monolith.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)

from codespine.analysis.community import detect_communities, symbol_community
from codespine.analysis.context import build_symbol_context
from codespine.analysis.coupling import get_coupling
from codespine.analysis.deadcode import detect_dead_code as detect_dead_code_analysis
from codespine.analysis.flow import trace_execution_flows as trace_flows_analysis
from codespine.analysis.impact import analyze_impact, resolve_symbol_targets

from codespine.mcp._helpers import (
    _index_guard,
    _json,
    _no_symbols_response,
    _normalize_symbol_input,
    _safe_tool_response,
    _staleness_meta,
    _overlay_snapshot_mtime,
)


def register_tools(mcp, store, repo_path_provider, telemetry, overlay_store, result_cache, watch, cache_key):
    """Register analysis-related tools on *mcp*."""

    @mcp.tool()
    def get_impact(symbol: str, max_depth: int = 4, project: str | None = None):
        """
        Caller-tree impact analysis for a symbol.

        Returns two sections:
          resolved_to     - the symbol(s) matched by name
          impacted_callers - BFS caller groups by depth (1 = direct, 2 = indirect, 3+ = transitive)
          self_callers    - methods in the same class that call the target (separated for clarity)

        Includes DI edges (@Inject/@Autowired/@Provides/@Bean) when the index has been
        built with a DI-aware version of CodeSpine.

        project scopes the target symbol lookup; cross-project callers are always included.
        """
        try:
            ck = cache_key(
                "get_impact",
                symbol=symbol,
                max_depth=max_depth,
                project=project,
                overlay_mtime=_overlay_snapshot_mtime(store, project),
            )
            cached = result_cache.get(ck)
            if cached is not None:
                return cached
            normalized = _normalize_symbol_input(symbol)
            result = analyze_impact(store, normalized, max_depth=max_depth, project=project)
            if not result.get("resolved_to"):
                result = analyze_impact(store, symbol, max_depth=max_depth, project=project)
            if not result.get("resolved_to"):
                return {"available": False, "note": f"Symbol '{symbol}' not found in the index."}
            out = _staleness_meta(
                store, {"available": True, **result}, project, overlay_store=overlay_store
            )
            result_cache.put(ck, out)
            return out
        except Exception as exc:
            return _safe_tool_response("get_impact", exc)

    @mcp.tool()
    def detect_dead_code(limit: int = 200, project: str | None = None, strict: bool = False):
        """
        Detect methods with no incoming calls (after Java-aware exemptions).
        Pass project to scope to a single module.

        Parameters:
          strict - When True, only main()/@Test and explicit entry-point
                   annotations are exempted. Constructors, getters/setters,
                   contract methods (toString, hashCode, equals), and method
                   overrides are NOT exempt. Use this for a thorough audit.
                   Each result includes a confidence level (high/medium/low):
                     high   = private method, almost certainly dead
                     medium = package-private or protected
                     low    = public method, could be called via reflection

        Returns dead_code list, count, and an exemption_stats dict showing
        how many candidates were found and how many were filtered out by the
        exemption rules - useful for validating that the feature is working
        even when the dead list is empty.
        """
        ck = cache_key("detect_dead_code", limit=limit, project=project, strict=strict)
        cached = result_cache.get(ck)
        if cached is not None:
            return cached

        raw = detect_dead_code_analysis(store, limit=limit, project=project, strict=strict)
        if raw is None:
            return _no_symbols_response()

        stats = {}
        dead = []
        for entry in raw:
            if "_stats" in entry:
                stats = entry["_stats"]
            else:
                dead.append(entry)

        out = _staleness_meta(
            store,
            {
                "available": True,
                "dead_code": dead,
                "count": len(dead),
                "exemption_stats": stats,
            },
            project,
            overlay_store=overlay_store,
            deep_scope=True,
        )
        result_cache.put(ck, out)
        return out

    # NOTE: trace_execution_flows is kept inline in server.py because its
    # parameter names and logic differ from the simple version.  Move here
    # once the inline version has been verified identical.
