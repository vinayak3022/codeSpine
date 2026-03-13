# CodeSpine

CodeSpine turns a Java codebase into a graph that coding agents can query over MCP.

It indexes classes, methods, calls, type relationships, cross-module links, git coupling, dead-code candidates, and execution flows so an agent can ask for structure directly instead of reading raw files first.

## Install

```bash
pip install codespine
```

Optional semantic search:

```bash
pip install "codespine[ml]"
```

## What It Does

- Hybrid search: BM25 + fuzzy by default, semantic vector search with `--embed`
- Impact analysis: callers, dependencies, and confidence-scored edges
- Dead code detection: Java-aware exemptions for tests, framework hooks, contracts, and common DI patterns
- Execution flows: traces from entry points through the call graph
- Community detection: structural clusters for architectural context
- Change coupling: git-history-based file relationships
- Multi-project and multi-module indexing: workspaces, Maven modules, Gradle subprojects
- MCP server: structured tools for Claude, Cursor, Cline, Copilot, and similar clients

## Quick Start

Index a repo:

```bash
codespine analyse /path/to/project
```

Run a deeper pass:

```bash
codespine analyse /path/to/project --deep
```

Add embeddings for semantic search:

```bash
codespine analyse /path/to/project --embed
```

Typical output:

```text
$ codespine analyse .
Walking files...               142 files found
Index mode...                  incremental (8 files to index, 0 deleted)
Parsing code...                8/8
Tracing calls...               847 calls resolved
Analyzing types...             234 type relationships
Cross-module linking...        skipped (single module)
Detecting communities...       8 clusters found
Detecting execution flows...   34 processes found
Finding dead code...           12 unreachable symbols
Analyzing git history...       18 coupled file pairs
Generating embeddings...       0 vectors stored

Done in 4.2s - 623 symbols, 1847 edges, 8 clusters, 34 flows (no embeddings; rerun with --embed for semantic search)
```

Search the index:

```bash
codespine search "retry payment"
codespine context "PaymentService"
codespine impact "com.example.PaymentService#charge(java.lang.String)"
codespine stats
```

## MCP

Foreground MCP server:

```bash
codespine mcp
```

Minimal MCP config:

```json
{
  "mcpServers": {
    "codespine": {
      "command": "codespine",
      "args": ["mcp"]
    }
  }
}
```

If the client launches the wrong Python environment, use the absolute binary path instead:

```json
{
  "mcpServers": {
    "codespine": {
      "command": "/absolute/path/to/codespine",
      "args": ["mcp"]
    }
  }
}
```

Common MCP tools:

- `search_hybrid(query, k, project)`
- `find_symbol(name, kind, project, limit)`
- `get_symbol_context(query, max_depth, project)`
- `get_impact(symbol, max_depth, project)`
- `detect_dead_code(limit, project, strict)`
- `trace_execution_flows(entry_symbol, max_depth, project)`
- `get_symbol_community(symbol)`
- `get_change_coupling(months, min_strength, min_cochanges, project)`
- `compare_branches(base_ref, head_ref)`
- `get_codebase_stats()`

## CLI

Core commands:

```bash
codespine analyse <path>
codespine analyse <path> --full
codespine analyse <path> --deep
codespine analyse <path> --embed
codespine watch --path .
codespine search "query"
codespine context "symbol"
codespine impact "symbol"
codespine deadcode
codespine flow
codespine community
codespine coupling
codespine diff main..feature
codespine stats
codespine list
codespine clear-project <project_id>
codespine clear-index
```

`analyse` defaults to incremental mode. Repeat runs are designed to be fast when files have not changed.

## Workspace And Module Detection

CodeSpine can index:

- a single Java repo
- a multi-module Maven or Gradle repo
- a workspace directory containing multiple repos

Project IDs are:

- single-module repo: `payments-service`
- multi-module repo: `payments-service::core`, `payments-service::api`

That same project ID can be passed into MCP tools and CLI analysis calls that support project scoping.

## Deep Analysis Trade-Offs

`--deep` enables the expensive graph-wide passes:

- communities
- execution flows
- dead code
- git coupling

Use it when you want architecture-level context. Skip it when you just need the graph refreshed for search, context, and impact.

`--embed` is also optional. Without it, CodeSpine still supports exact, keyword, and fuzzy search. Add embeddings when you need concept-level retrieval.

## Runtime Files

- `~/.codespine_db` - graph database
- `~/.codespine.pid` - MCP background server PID
- `~/.codespine.log` - server log
- `~/.codespine_embedding_cache.json` - embedding cache
- `~/.codespine_index_meta/` - incremental file metadata cache

## Notes

- `codespine start` launches a background MCP server. Most IDE MCP clients should use `codespine mcp` instead and manage the process themselves.
- `codespine clear-index` rebuilds the local index database from scratch.
- For large Spring or JPA-heavy repos, dead-code results should still be reviewed before deletion. The tool is conservative, not authoritative.

## Project Docs

- [Contributing](.github/CONTRIBUTING.md)
- [Security](.github/SECURITY.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
