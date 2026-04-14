# CodeSpine

CodeSpine cuts token burn for coding agents working on Java codebases.

Instead of having an agent open dozens of `.java` files to answer one question, CodeSpine indexes the codebase once and serves the structure over MCP. The agent asks for symbols, callers, impact, flows, dead code, and module boundaries directly, which means fewer file reads, fewer wasted context windows, and fewer hallucinated code paths.

It indexes classes, methods, calls, type relationships, DI bindings, cross-module links, git coupling, dead-code candidates, and execution flows so agents can work from graph answers first and source files second.

File changes are written directly to the graph and are immediately queryable — no stale overlay merging, no OOM accumulation. The MCP daemon reloads from an atomic read replica the moment indexing or watch mode completes a batch.

The MCP daemon and the indexer run independently. Querying while a full re-index is running no longer causes crashes or memory contention — reads go to an isolated snapshot that is atomically updated when indexing completes.

## Why It Saves Tokens

- One MCP call can replace many file opens. `get_symbol_context("PaymentService")` returns a resolved neighborhood instead of forcing the agent to read every caller and callee file manually.
- Search is structure-aware. Agents can ask for a symbol, concept, impact radius, or dead-code candidate without scanning entire packages.
- DI bindings are first-class. `@Inject`, `@Autowired`, `@Bean`, and `@Provides` edges are resolved and included in impact analysis — Spring and Guice consumers are never missed.
- Multi-module repos stay scoped. Project-aware IDs and `project=` parameters reduce noise from unrelated modules and workspaces.
- Repeat sessions get cheaper. Once indexed, the agent reuses the graph instead of re-discovering the same relationships every turn.
- Active edits are visible immediately. Watch mode writes changes directly to the graph (not a slow overlay), so every MCP query reflects the latest file save.

## Install

```bash
pip install codespine
```

Optional semantic search:

```bash
pip install "codespine[ml]"
```

## What It Does

- Hybrid search: BM25 + fuzzy by default, semantic vector search with `--embed`; results carry `high/medium/low` confidence scores
- Impact analysis: callers, DI consumers, and confidence-scored edges; same-class callers separated from cross-class ones
- DI analysis: `@Inject`/`@Autowired`/`@Bean`/`@Provides` edges resolved into `INJECTS` + `BINDS_INTERFACE` graph relationships
- Dead code detection: Java-aware exemptions for tests, framework hooks, contracts, and common DI patterns
- Execution flows: traces from entry points through the call graph
- Community detection: structural clusters for architectural context
- Change coupling: git-history-based file relationships
- Multi-project and multi-module indexing: workspaces, Maven modules, Gradle subprojects
- Cross-module call linking: signature-based detection of calls between Maven/Gradle modules
- Concurrent read/write isolation: MCP queries run against a read replica; the indexer writes separately, with no memory contention
- Git commit hook: optional post-commit hook re-indexes only the changed files within seconds
- MCP server: 44 structured tools for Claude, Cursor, Cline, Copilot, and similar clients

## Instant Change Visibility

CodeSpine writes file changes directly to the graph — no O(N) overlay merging on every query.

When `codespine watch` detects a file save:
1. Parses the changed file with tree-sitter
2. Atomically clears then re-writes that file's methods, calls, and type relationships
3. Snapshots the write DB to the read replica
4. The MCP server picks up the new snapshot on its next tool call

The result is that every tool — `search_hybrid`, `get_impact`, `get_symbol_context`, `find_injections`, and all others — reflects unsaved work within the debounce window (default 1–2 s).

### Git Commit Auto Re-index

Watch mode polls `git HEAD` every 5 seconds. When HEAD changes it uses `git diff --name-only` to find only the modified Java files and re-indexes those — not the entire project.

You can also install an optional post-commit hook so re-indexing fires immediately on every commit:

```bash
codespine watch --path . --install-hook
```

Or via MCP:

```python
start_watch(path=".", install_hook=True)
```

The hook is idempotent and can be removed with:

```bash
codespine watch --uninstall-hook --path .
```

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
Analyzing DI bindings...       63 INJECTS edges, 14 BINDS_INTERFACE edges
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

### Agent Onboarding

When an agent connects to CodeSpine for the first time, it should call:

1. **`guide()`** — returns a structured catalog of every tool, organized by category, with recommended workflows and tips.
2. **`get_capabilities()`** — returns what is indexed right now, which features are ready, and what's missing.

The same information is available from the CLI:

```bash
codespine guide          # tool catalog, workflows, tips
codespine guide --json   # structured JSON for tooling
```

### MCP Tools

**Discovery & Status**

| Tool | Description |
|------|-------------|
| `guide()` | Tool catalog, workflows, and tips. Call first if new to CodeSpine. |
| `get_capabilities()` | What is indexed and which features are available right now. |
| `list_projects()` | All indexed projects with symbol/file counts. |
| `get_codebase_stats()` | Per-project stats: files, classes, methods, call edges, embeddings. |
| `list_packages(project)` | Java packages in the index. |
| `ping()` | Verify the MCP server is alive. |

**Search & Lookup**

| Tool | Description |
|------|-------------|
| `search_hybrid(query, k, project)` | Ranked symbol search (BM25 + vector + fuzzy via RRF) with confidence scores. |
| `find_symbol(name, kind, project, limit)` | Exact/prefix name lookup; returns `primary_match` flag and disambiguated results. |
| `get_symbol_context(query, max_depth, project)` | One-shot deep context: search + impact + community + flows. |
| `get_neighborhood(symbol, project)` | Callers, callees, siblings, and override/implements. |

**Analysis**

| Tool | Description |
|------|-------------|
| `get_impact(symbol, max_depth, project)` | Caller-tree impact analysis including DI consumers. Same-class callers in `self_callers`; cross-class in `impacted_callers`. |
| `find_injections(symbol, project)` | All classes that `@Inject`/`@Autowired` a given type, and all `@Bean`/`@Provides` providers. |
| `detect_dead_code(limit, project, strict)` | Methods with no callers (Java-aware exemptions). |
| `trace_execution_flows(entry_symbol, max_depth, project)` | Execution paths from entry points. |
| `get_symbol_community(symbol)` | Architectural community cluster for a symbol. |
| `get_change_coupling(days, min_strength, min_cochanges)` | Files that changed together in the last N days (default 5). |

**LLM-Native Tools**

Higher-level tools designed to answer full agent questions in a single call:

| Tool | Description |
|------|-------------|
| `ask(question, project)` | Answer a free-form question about the codebase using indexed structure. |
| `what_breaks(symbol, project)` | Plain-English summary of what could break if this symbol changes. |
| `explain(symbol, project)` | Explain what a class or method does and how it fits in the architecture. |
| `read_symbols(symbols, project)` | Bulk-resolve a list of symbol names to context in one call. |
| `semantic_summary(query, project)` | Narrative summary of modules or concepts matching a query. |
| `get_api_surface(project)` | All public entry points: REST controllers, gRPC services, CLI commands. |
| `file_context(file_path, project)` | Everything known about a file: classes, methods, callers, type deps. |
| `pre_flight_check(project)` | Readiness report: index freshness, coverage, missing embeddings, DI gaps. |
| `related(symbol, project)` | Symbols structurally or semantically related to the given one. |
| `test_coverage(symbol, project)` | Test classes and methods that exercise the given symbol. |
| `diff_impact(base_ref, head_ref, project)` | Impact analysis scoped to the symbols changed between two git refs. |
| `find_pattern(pattern, project)` | Find code matching a structural or naming pattern across the codebase. |

**Git**

| Tool | Description |
|------|-------------|
| `git_log(file_path, limit, project)` | Recent git commits. |
| `git_diff(ref, file_path, project)` | Git diff (working tree vs ref, or between refs). |
| `compare_branches(base_ref, head_ref, project)` | Symbol-level diff between two git refs. |

**Indexing & Watch**

| Tool | Description |
|------|-------------|
| `analyse_project(path, full, deep, embed)` | Index a Java project (background job). |
| `get_analyse_status()` | Poll analysis progress. |
| `reindex_file(file_path, project)` | Re-index a single `.java` file (<1 s). Changes are immediately queryable. |
| `start_watch(path, install_hook)` | Watch for `.java` changes and write directly to graph in real time. Pass `install_hook=True` to also install a post-commit git hook. |
| `stop_watch()` | Stop the background watch process. |
| `get_watch_status()` | Watch mode status: running, path, uptime. |

**Overlay**

| Tool | Description |
|------|-------------|
| `get_overlay_status(project)` | Uncommitted overlay state by project/module. |
| `promote_overlay(project)` | Commit dirty overlay into the base index. |
| `clear_overlay(project)` | Discard dirty overlay without changing the base. |

**Reset**

| Tool | Description |
|------|-------------|
| `reset_project(project_id)` | Remove all data for one project. |
| `reset_index()` | Remove ALL data across every project. |
| `force_reset_index()` | Emergency: delete data files when normal reset fails. |

**Advanced**

| Tool | Description |
|------|-------------|
| `run_cypher(query)` | Run a raw Cypher query against the graph DB. |

## CLI

```bash
# Indexing
codespine analyse <path>                     # incremental index
codespine analyse <path> --full              # full re-index
codespine analyse <path> --deep              # + communities, flows, dead code, coupling
codespine analyse <path> --embed             # + vector embeddings
codespine watch --path .                     # live re-index on file changes (direct-to-graph)
codespine watch --path . --install-hook      # also install post-commit git hook

# Search & Analysis
codespine search "query"                     # hybrid search
codespine context "symbol"                   # one-shot deep context
codespine impact "symbol"                    # caller-tree impact (includes DI consumers)
codespine deadcode                           # dead code candidates
codespine flow                               # execution flows
codespine community                          # architectural clusters
codespine coupling                           # git change coupling
codespine diff main..feature                 # symbol-level branch diff

# Status & Info
codespine stats                              # per-project statistics
codespine list                               # indexed projects
codespine status                             # service and database status
codespine guide                              # tool catalog and workflows

# Overlay
codespine overlay-status                     # dirty overlay state
codespine overlay-promote                    # commit overlay to base
codespine overlay-clear                      # discard overlay

# Server Management
codespine start                              # launch background MCP server
codespine stop                               # stop background MCP server
codespine mcp                                # foreground MCP (stdio, for IDE)

# Cleanup & Reset
codespine clear-project <project_id>         # remove one project
codespine clear-index                        # remove all indexed data
codespine force-reset                        # emergency: delete all data files
codespine setup                              # check dependencies
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

## DI / Injection Analysis

CodeSpine resolves dependency injection bindings at index time and stores them as first-class graph edges.

**What is indexed:**

- `@Inject` / `@Autowired` fields → `INJECTS(consumer → provider, confidence=0.85)`
- `@Provides` / `@Bean` methods → `INJECTS(config → return_type, confidence=0.90)`
- `@Component` / `@Service` implementing an interface → `BINDS_INTERFACE(impl → interface, confidence=0.95)`

**How it affects existing tools:**

- `get_impact("PaymentService")` now includes all classes that inject `PaymentService`, not just direct callers.
- `detect_dead_code` skips classes referenced only via DI edges.

**New tool:**

```python
find_injections("PaymentProcessor")
# → all @Inject/@Autowired consumers
# → all @Bean/@Provides providers
# → all @Component/@Service implementations of the interface
```

## Deep Analysis Trade-Offs

`--deep` enables the expensive graph-wide passes:

- communities
- execution flows
- dead code
- git coupling

Use it when you want architecture-level context. Skip it when you just need the graph refreshed for search, context, and impact.

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
- `~/.codespine_overlay/` - uncommitted dirty overlay state (legacy; direct-to-graph is now the primary path)

## Notes

- `codespine start` launches a background MCP server. Most IDE MCP clients should use `codespine mcp` instead and manage the process themselves.
- `codespine watch` writes changes directly to the graph and snapshots the read replica after each batch. MCP queries reflect file saves within the debounce window.
- `git HEAD` is polled every 5 seconds. On a new commit, only the changed Java files are re-indexed using `git diff --name-only`, not the full project.
- `codespine clear-index` rebuilds the local index database from scratch. This also removes the read replica; run `analyse` again to republish it.
- `codespine force-reset` is the nuclear option — it deletes all data files without going through the DB engine. Use it when `clear-index` fails due to DB corruption.
- For large Spring or JPA-heavy repos, dead-code results should still be reviewed before deletion. The tool is conservative, not authoritative.

## Project Docs

- [Contributing](.github/CONTRIBUTING.md)
- [Security](.github/SECURITY.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
