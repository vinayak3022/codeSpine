"""CodeSpine guide – single source of truth for MCP + CLI."""

from __future__ import annotations

from codespine import __version__

GUIDE_SECTIONS: list[dict] = [
    {
        "id": "overview",
        "title": "What is CodeSpine?",
        "body": (
            "CodeSpine is a Java code-intelligence engine. It parses Java source "
            "into a graph database (DuckDB by default, Kuzu as an alternate backend) and exposes hybrid search, impact analysis, "
            "dead-code detection, execution-flow tracing, community detection, and "
            "git change-coupling -- all via MCP tools or CLI commands.\n"
            "Language: Java. Layouts: single-module, multi-module (Maven/Gradle), workspaces."
        ),
    },
    {
        "id": "quickstart",
        "title": "Recommended First Steps",
        "body": (
            "1. Call get_capabilities() to see what is indexed and which features are ready.\n"
            "2. If nothing is indexed: call analyse_project(path) with the Java project root.\n"
            "   Poll with get_analyse_status() until it finishes.\n"
            "3. Once indexed: use search_hybrid(query, project=..., explain=True) to find symbols, then drill in with\n"
            "   get_impact(), get_symbol_context(), or get_neighborhood().\n"
            "4. For active development: call start_watch(path) to keep the index fresh\n"
            "   as files change on disk."
        ),
    },
    # ── Tool catalog ──────────────────────────────────────────────
    {
        "id": "tools_discovery",
        "title": "Discovery & Status",
        "tools": [
            {"name": "get_capabilities", "one_liner": "What is indexed and which features are available right now. Call this first."},
            {"name": "list_projects", "one_liner": "All indexed projects with symbol/file counts."},
            {"name": "get_codebase_stats", "one_liner": "Per-project stats: files, classes, methods, call edges, embeddings."},
            {"name": "list_packages", "one_liner": "Java packages in the index (optional project= filter)."},
            {"name": "ping", "one_liner": "Verify the MCP server is alive."},
        ],
    },
    {
        "id": "tools_search",
        "title": "Search & Lookup",
        "tools": [
            {"name": "search_hybrid", "one_liner": "Ranked symbol search (BM25 + vector + fuzzy via RRF). Pass project= or explain=True for scoped, provenance-aware retrieval."},
            {"name": "find_symbol", "one_liner": "Exact/prefix name lookup. Use to resolve ambiguity or list overloads."},
            {"name": "get_symbol_context", "one_liner": "One-shot deep context: search + impact + community + flows in one call."},
            {"name": "get_neighborhood", "one_liner": "Callers, callees, siblings, and override/implements for a symbol."},
        ],
    },
    {
        "id": "tools_analysis",
        "title": "Analysis",
        "body": "Some analyses require 'analyse_project(path, deep=True)' to populate data.",
        "tools": [
            {"name": "get_impact", "one_liner": "Caller-tree impact analysis grouped by depth with confidence scores."},
            {"name": "detect_dead_code", "one_liner": "Methods with no callers (Java-aware exemptions). strict=True for thorough audit."},
            {"name": "trace_execution_flows", "one_liner": "Execution paths from entry points (main methods, tests, controllers)."},
            {"name": "get_symbol_community", "one_liner": "Architectural community cluster a symbol belongs to."},
            {"name": "get_change_coupling", "one_liner": "Files that changed together in the last N days (default 5). git co-change analysis."},
        ],
    },
    {
        "id": "tools_git",
        "title": "Git Integration",
        "tools": [
            {"name": "git_log", "one_liner": "Recent git commits for a project or a specific file."},
            {"name": "git_diff", "one_liner": "Git diff (working tree vs ref, or between two refs)."},
            {"name": "compare_branches", "one_liner": "Symbol-level diff between two git refs (branches/tags/commits)."},
        ],
    },
    {
        "id": "tools_indexing",
        "title": "Indexing & Watch",
        "tools": [
            {"name": "analyse_project", "one_liner": "Index a Java project (background). Poll get_analyse_status() for progress."},
            {"name": "get_analyse_status", "one_liner": "Status of current/recent background analysis job."},
            {"name": "reindex_file", "one_liner": "Re-index a single .java file via overlay (<1 s)."},
            {"name": "start_watch", "one_liner": "Watch a directory for .java changes; updates overlay in real time."},
            {"name": "stop_watch", "one_liner": "Stop the background watch process."},
            {"name": "get_watch_status", "one_liner": "Is watch mode running? Path, uptime."},
        ],
    },
    {
        "id": "tools_overlay",
        "title": "Overlay (Dirty-State Tracking)",
        "body": (
            "The overlay tracks uncommitted file changes separately from the base index.\n"
            "search_hybrid and find_symbol merge both layers automatically.\n"
            "Deep analyses (dead code, flows, communities) use the committed base only."
        ),
        "tools": [
            {"name": "get_overlay_status", "one_liner": "Uncommitted overlay state by project/module."},
            {"name": "promote_overlay", "one_liner": "Commit dirty overlay into the base index immediately."},
            {"name": "clear_overlay", "one_liner": "Discard dirty overlay without changing the base index."},
        ],
    },
    {
        "id": "tools_reset",
        "title": "Index Reset",
        "tools": [
            {"name": "reset_project", "one_liner": "Remove all data for one project. Re-index with analyse_project()."},
            {"name": "reset_index", "one_liner": "Remove ALL data across every project (clean slate)."},
            {"name": "force_reset_index", "one_liner": "Emergency: delete data files when normal reset fails (OOM/corruption)."},
        ],
    },
    {
        "id": "tools_advanced",
        "title": "Advanced",
        "tools": [
            {"name": "run_cypher", "one_liner": "Run a raw Cypher query against the graph DB."},
        ],
    },
    # ── Workflows ─────────────────────────────────────────────────
    {
        "id": "workflows",
        "title": "Common Workflows",
        "body": (
            "Understand a symbol:\n"
            "  search_hybrid('PaymentService') -> get_impact('processPayment') -> get_neighborhood('processPayment')\n"
            "\n"
            "Find dead code:\n"
            "  detect_dead_code(strict=True) -> get_impact(candidate) to verify each hit\n"
            "\n"
            "Review a branch:\n"
            "  compare_branches('main', 'feature-x') -> get_impact() on each changed symbol\n"
            "\n"
            "Active development:\n"
            "  analyse_project(path) -> start_watch(path) -> search/analyse as needed\n"
            "\n"
            "Explore architecture:\n"
            "  list_packages() -> get_symbol_community(symbol) -> trace_execution_flows()"
        ),
    },
    {
        "id": "tips",
        "title": "Tips",
        "body": (
            "- Most tools accept an optional project= parameter to scope results.\n"
            "  Use list_projects() to see available project IDs.\n"
            "- Multi-module projects use IDs like 'myapp::core', 'myapp::web'.\n"
            "- get_symbol_context() is the best single call for understanding any symbol.\n"
            "- For git tools, pass project= so the correct repo root is resolved.\n"
            "- BM25 + fuzzy search works without embeddings. Semantic search needs\n"
            "  'pip install codespine[ml]' and 'analyse_project(path, embed=True)'.\n"
            "- Use search_hybrid(query, project=..., explain=True) when you want ranker\n"
            "  traces, match reasons, and confidence explanations.\n"
            "- If community/flow/coupling data is missing, re-run:\n"
            "  analyse_project(path, deep=True)"
        ),
    },
]


def format_guide_terminal() -> str:
    """Render GUIDE_SECTIONS as formatted terminal text."""
    lines: list[str] = [f"CodeSpine v{__version__} -- Guide\n"]
    for section in GUIDE_SECTIONS:
        lines.append(f"{'=' * 64}")
        lines.append(f"  {section['title']}")
        lines.append(f"{'=' * 64}")
        if "body" in section:
            lines.append(section["body"])
        if "tools" in section:
            for t in section["tools"]:
                lines.append(f"  {t['name']:<28} {t['one_liner']}")
        lines.append("")
    return "\n".join(lines)
