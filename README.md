# CodeSpine

CodeSpine cuts token burn for coding agents working on Java codebases.

Instead of having an agent open dozens of `.java` files to answer one question, CodeSpine indexes the codebase once and serves the structure over MCP. The agent asks for symbols, callers, impact, flows, dead code, and module boundaries directly, which means fewer file reads, fewer wasted context windows, and fewer hallucinated code paths.

It indexes classes, methods, calls, type relationships, cross-module links, git coupling, dead-code candidates, and execution flows so agents can work from graph answers first and source files second.

It also keeps a separate dirty overlay for uncommitted Java edits, so agents can query current work-in-progress without forcing the committed base index to churn on every save.

The MCP daemon and the indexer run independently. Querying while a full re-index is running no longer causes crashes or memory contention — reads go to an isolated snapshot that is atomically updated when indexing completes.

## Why It Saves Tokens

- One MCP call can replace many file opens. `get_symbol_context("PaymentService")` returns a resolved neighborhood instead of forcing the agent to read every caller and callee file manually.
- Search is structure-aware. Agents can ask for a symbol, concept, impact radius, or dead-code candidate without scanning entire packages.
- Multi-module repos stay scoped. Project-aware IDs and `project=` parameters reduce noise from unrelated modules and workspaces.
- Repeat sessions get cheaper. Once indexed, the agent reuses the graph instead of re-discovering the same relationships every turn.
- Active edits stay smooth. Dirty files are kept in an overlay and merged into fast queries until you commit, instead of hammering the main graph DB on each change.

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
- Cross-module call linking: signature-based detection of calls between Maven/Gradle modules
- Concurrent read/write isolation: MCP queries run against a read replica; the indexer writes separately, with no memory contention
- MCP server: structured tools for Claude, Cursor, Cline, Copilot, and similar clients

## Editing Without Stale Indexes

CodeSpine uses a two-layer model:

- Base index: last committed state
- Dirty overlay: uncommitted Java changes

Fast tools read merged `base + overlay` state by default:

- `search`
- `context`
- `impact`
- MCP `search_hybrid`
- MCP `find_symbol`
- MCP `get_symbol_context`
- MCP `get_impact`

Deep analyses stay committed-only until promotion:

- `deadcode`
- `flow`
- `community`
- `coupling`

`codespine watch` updates the dirty overlay after a debounce window, then promotes it into the base index when local `HEAD` changes.

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
Detecting communities...       loading symbols
Detecting communities...       623 symbols, 1204 structural edges
Detecting communities...       persisting 8/8 clusters
Detecting communities...       8 clusters found
Detecting execution flows...   34 entry points, tracing
Detecting execution flows...   34 processes found
Finding dead code...           12 unreachable symbols
Analyzing git history...       18 commits, computing co-changes
Analyzing git history...       18 coupled file pairs
Generating embeddings...       0 vectors stored

Done in 4.2s - 623 symbols, 1847 edges, 8 clusters, 34 flows (no embeddings; rerun with --embed for semantic search)
Publishing read replica...     MCP will reload automatically
```

Each analysis phase streams live progress in place. The final step publishes a read replica so the MCP daemon picks up the new index without restarting.

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
codespine watch --path . --overlay-debounce-ms 1500
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
codespine overlay-status
codespine overlay-promote
codespine overlay-clear
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

When a dirty overlay exists, deep-analysis results intentionally exclude those uncommitted edits until promotion.

`--embed` is also optional. Without it, CodeSpine still supports exact, keyword, and fuzzy search. Add embeddings when you need concept-level retrieval.

## Concurrent Indexing and Querying

The indexer (write) and the MCP daemon (read) use separate database paths:

- The indexer writes to `~/.codespine_db` with a 512 MB buffer pool.
- When indexing completes, `analyse` atomically copies the database to `~/.codespine_db_read` and touches a sentinel file.
- The MCP daemon and all read-only CLI commands open `~/.codespine_db_read` with a 128 MB buffer pool.
- The MCP daemon watches the sentinel file and silently reloads from the new snapshot on the next tool call — no restart needed.

Running `codespine analyse --deep --embed` on one project while querying a different one no longer causes buffer pool OOM or lock contention.

## Runtime Files

- `~/.codespine_db` - graph database (write)
- `~/.codespine_db_read` - read replica used by MCP and CLI queries
- `~/.codespine_db_read.updated` - sentinel file; touched after each successful snapshot
- `~/.codespine.pid` - MCP background server PID
- `~/.codespine.log` - server log
- `~/.codespine_embedding_cache.json` - embedding cache
- `~/.codespine_index_meta/` - incremental file metadata cache
- `~/.codespine_overlay/` - uncommitted dirty overlay state

## Notes

- `codespine start` launches a background MCP server. Most IDE MCP clients should use `codespine mcp` instead and manage the process themselves.
- `codespine watch` updates the dirty overlay first; it does not rewrite the committed base index on every save.
- `codespine clear-index` rebuilds the local index database from scratch. This also removes the read replica; run `analyse` again to republish it.
- For large Spring or JPA-heavy repos, dead-code results should still be reviewed before deletion. The tool is conservative, not authoritative.
- The first run after upgrading to v0.5.7 will not have a read replica yet. Run `codespine analyse` once to create it.

## Project Docs

- [Contributing](.github/CONTRIBUTING.md)
- [Security](.github/SECURITY.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
