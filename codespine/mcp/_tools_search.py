"""
Search tools for the CodeSpine MCP server.

Extracted from ``server.py`` to break up the monolith.

NOTE: Only ``search_hybrid`` is currently extracted here.  The other search
tools (``find_symbol``, ``get_codebase_stats``, ``list_packages``,
``run_cypher``) are kept inline in ``server.py`` until they can be verified
identical and their monkeypatch targets are updated in the test suite.
"""

from __future__ import annotations

import logging
import os

_LOGGER = logging.getLogger(__name__)

from codespine.search.hybrid import hybrid_search
from codespine.mcp._helpers import (
    _index_guard,
    _no_symbols_response,
    _staleness_meta,
)


def register_tools(mcp, store, repo_path_provider, telemetry, overlay_store, result_cache, watch, cache_key):
    """Register search-related tools on *mcp*."""

    @mcp.tool()
    def search_hybrid(
        query: str,
        k: int = 20,
        project: str | None = None,
        explain: bool = False,
        detail: str = "full",
        pool_size: int | None = None,
    ):
        """
        Hybrid symbol search (BM25 + vector + fuzzy, fused with RRF).
        Pass project=<project_id> to scope results to a single indexed project.
        Pass explain=True to include retrieval traces, match reasons, and confidence notes.
        Pass detail='compact' to skip architectural context and snippets unless explicitly requested.
        Pass pool_size to override the semantic candidate pool (default: config value).
        Use list_projects to see available project IDs.
        """
        guard = _index_guard(store)
        if guard is not None:
            return guard
        _pool = pool_size or int(os.environ.get("CODESPINE_CANDIDATE_POOL", "0")) or None
        results = hybrid_search(
            store, query, k=k, project=project, explain=explain, detail=detail, pool_size=_pool
        )
        if not results:
            return _no_symbols_response()
        payload = (
            {"available": True, **results}
            if explain and isinstance(results, dict)
            else {"available": True, "results": results}
        )
        return _staleness_meta(store, payload, project, overlay_store=overlay_store)
