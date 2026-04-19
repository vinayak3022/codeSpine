# CodeSpine

**v1.0.0** — Local Java code intelligence for coding agents, backed by a graph database.

CodeSpine cuts token burn for coding agents working on Java codebases.

Instead of having an agent open dozens of `.java` files to answer one question, CodeSpine indexes the codebase once and serves the structure over MCP. The agent asks for symbols, callers, impact, flows, dead code, and module boundaries directly — fewer file reads, fewer wasted context windows, fewer hallucinated code paths.

It indexes classes, methods, calls, type relationships, DI bindings, cross-module links, git coupling, dead-code candidates, and execution flows so agents work from graph answers first and source files second.

File changes are written directly to the graph and are immediately queryable — no stale overlay merging, no OOM accumulation. The MCP daemon reloads from an atomic read replica the moment indexing or watch mode completes a batch.

---

## Why It Saves Tokens

- **One MCP call replaces many file opens.** `get_symbol_context("PaymentService")` returns a resolved neighborhood instead of forcing the agent to read every caller and callee file manually.
- **Search is structure-aware.** Ask for a symbol, concept, impact radius, or dead-code candidate without scanning entire packages.
- **DI bindings are first-class.** `@Inject`, `@Autowired`, `@Bean`, and `@Provides` edges are resolved and included in impact analysis — Spring and Guice consumers are never missed.
- **Multi-module repos stay scoped.** Project-aware IDs and `project=` parameters reduce noise from unrelated modules and workspaces.
- **Repeat sessions get cheaper.** Once indexed, the agent reuses the graph instead of re-discovering the same relationships every turn.
- **Active edits are visible immediately.** Watch mode writes changes directly to the graph (not a slow overlay), so every MCP query reflects the latest file save.
- **Natural language dispatch.** `ask("what breaks if I change PaymentService?")` routes to the right tool automatically, reducing agent planning overhead.

---

## Install

```bash
pip install codespine
```

Optional semantic search (sentence-transformers):

```bash
pip install "codespine[ml]"
```

Everything at once (ml + community detection):

```bash
pip install "codespine[full]"
```

### One-time model download (for semantic search)

```bash
codespine install-model
```

Downloads and caches the embedding model. Only needed once. After this, `--embed` works without any network access.

---

## Quick Start

```bash
# 1. Index a project
codespine analyse /path/to/java-project

# 2. (Optional) Run the expensive deep passes: communities, flows, dead code, coupling
#    Auto-enabled for repos with ≤ 3,000 files; use --deep to force on larger repos.
codespine analyse /path/to/java-project --deep

# 3. (Optional) Add semantic embeddings for concept-level search
codespine analyse /path/to/java-project --embed

# 4. Start MCP server (foreground; your IDE manages the process)
codespine mcp
```

Typical output:

```
$ codespine analyse .
Walking files...               142 files found
Index mode...                  incremental (8 files to index, 0 deleted)
Parsing code...                8/8
Tracing calls...               847 calls resolved
Analyzing DI bindings...       63 INJECTS edges, 14 BINDS_INTERFACE edges
Analyzing types...             234 type relationships
Cross-module linking...        skipped (single module)
Detecting communities...       8 clusters found
Detecting execution flows...   34 processes found
Finding dead code...           12 unreachable symbols
Analyzing git history...       18 coupled file pairs
Generating embeddings...       623 vectors stored

Done in 4.2s — 623 symbols, 1,847 edges, 8 clusters, 34 flows
Publishing read replica...     MCP will reload automatically
```

Each analysis phase streams live progress. The final step publishes a read replica so the MCP daemon picks up the new index without restarting.

---

## MCP Configuration

Foreground server:

```bash
codespine mcp
```

Minimal `mcp.json` / Claude Desktop config:

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

If the client launches the wrong Python environment, use the absolute binary path:

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

When an agent connects for the first time:

1. **`guide()`** — structured catalog of every tool, by category, with recommended workflows and tips.
2. **`get_capabilities()`** — what is indexed right now, which features are ready, and what is missing.

The same information is available from the CLI:

```bash
codespine guide          # tool catalog, workflows, tips
codespine guide --json   # structured JSON for tooling
```

---

## MCP Tools (45 total)

### Discovery & Status

| Tool | Description |
|------|-------------|
| `guide()` | Tool catalog, workflows, and tips. Call first if new to CodeSpine. |
| `get_capabilities()` | What is indexed and which features are available right now. |
| `list_projects()` | All indexed projects with symbol/file counts. |
| `get_codebase_stats()` | Per-project stats: files, classes, methods, call edges, embeddings. |
| `list_packages(project)` | Java packages in the index. |
| `ping()` | Verify the MCP server is alive. |

### Search & Lookup

| Tool | Description |
|------|-------------|
| `search_hybrid(query, k, project)` | Ranked symbol search (BM25 + vector + fuzzy via RRF) with `high/medium/low` confidence scores. |
| `find_symbol(name, kind, project, limit)` | Exact/prefix name lookup; returns `primary_match` flag and disambiguated overloads. |
| `get_symbol_context(query, max_depth, project)` | One-shot deep context: search + impact + community + flows. |
| `get_neighborhood(symbol, project)` | Callers (same project), `cross_project_callers` (other projects), callees, siblings, and override/implements links. |

### Analysis

| Tool | Description |
|------|-------------|
| `get_impact(symbol, max_depth, project)` | Caller-tree BFS including DI consumers. `self_callers` separates same-class callers from `impacted_callers`. Cached for 5 min. |
| `find_injections(symbol, project)` | All `@Inject`/`@Autowired` consumers, `@Bean`/`@Provides` providers, and `@Component`/`@Service` implementations. |
| `detect_dead_code(limit, project, strict)` | Methods with no callers (Java-aware exemptions for tests, contracts, DI entry points). Cached for 5 min. |
| `trace_execution_flows(entry_symbol, max_depth, project)` | Execution paths from entry points through the call graph. |
| `get_symbol_community(symbol)` | Architectural community cluster for a symbol. |
| `get_change_coupling(days, min_strength, min_cochanges)` | Files that changed together in git history (default last 5 days). |

### LLM-Native Tools

Higher-level tools designed to answer full agent questions in a single call, without the agent needing to know which underlying tool to call:

| Tool | Description |
|------|-------------|
| `ask(question, project)` | Keyword-based natural language dispatcher: routes "who calls X", "what breaks if Y", "explain Z", "find methods named …" to the right tool automatically. |
| `what_breaks(symbol, project)` | Plain-English blast-radius summary with `risk_level` (low / medium / high). |
| `explain(symbol, project)` | What a class or method does and how it fits in the architecture. |
| `read_symbols(file, symbols, project)` | Extract only the requested method source ranges from a file using tree-sitter — 60–70% token reduction vs. reading the whole file. |
| `semantic_summary(symbol, project)` | Condensed class view: name, package, extends, implements, public method signatures, annotations. ~80 tokens vs. ~800. |
| `get_api_surface(class_name, project)` | Public methods and fields only. |
| `file_context(file_path, project)` | Symbols in a file, callers/callees, community, co-change partners. |
| `pre_flight_check(file, symbols, change_type)` | Blast-radius check before writing: runs `get_impact` per symbol, returns total affected + risk level + test gap. |
| `related(symbol, limit, project)` | Symbols structurally related via co-change coupling, shared community, direct calls, or class siblings. |
| `rename_plan(symbol, new_name, project)` | **Safe cross-project rename plan.** Finds all declaration sites, call sites, and override sites and returns a `files_to_modify` list. No files are modified. |
| `test_coverage(symbol, project)` | Test methods that cover the given symbol (direct or depth-2 calls from `@Test` methods). |
| `diff_impact(git_ref, project)` | Graph-level impact analysis for all Java symbols changed since `git_ref`. Returns risk level and per-file affected counts. |
| `find_pattern(description, project)` | Structural and semantic pattern matching across the codebase. |

### Git

| Tool | Description |
|------|-------------|
| `git_log(file_path, limit, project)` | Recent git commits for a path or project. |
| `git_diff(ref, file_path, project)` | Git diff (working tree vs. ref, or between two refs). |
| `compare_branches(base_ref, head_ref, project)` | Symbol-level diff between two git refs. |

### Indexing & Watch

| Tool | Description |
|------|-------------|
| `analyse_project(path, full, deep, embed)` | Index a Java project (background subprocess). |
| `get_analyse_status()` | Poll background analysis progress (includes last 30 log lines). |
| `reindex_file(file_path, project)` | Re-index a single `.java` file (<1 s). Changes are immediately queryable. |
| `start_watch(path, install_hook)` | Watch for `.java` changes; write directly to graph. Pass `install_hook=True` to also install a post-commit git hook. |
| `stop_watch()` | Stop the background watch process. |
| `get_watch_status()` | Watch mode status: running, path, uptime. |

> **Auto-watch:** The MCP server automatically starts watching the most-recently-indexed project on startup if watch is not already running.

### Overlay

| Tool | Description |
|------|-------------|
| `get_overlay_status(project)` | Uncommitted overlay state by project/module. |
| `promote_overlay(project)` | Commit dirty overlay into the base index. |
| `clear_overlay(project)` | Discard dirty overlay without changing the base. |

### Reset

| Tool | Description |
|------|-------------|
| `reset_project(project_id)` | Remove all data for one project. |
| `reset_index()` | Remove ALL data across every project. |
| `force_reset_index()` | Emergency: delete data files when normal reset fails. |

### Advanced

| Tool | Description |
|------|-------------|
| `run_cypher(query)` | Run a raw Cypher query against the graph DB. |

---

## CLI Reference

```bash
# Indexing
codespine analyse <path>                     # incremental index (default)
codespine analyse <path> --full              # full re-index from scratch
codespine analyse <path> --deep              # + communities, flows, dead code, coupling
codespine analyse <path> --incremental-deep  # incremental index + force deep passes
codespine analyse <path> --embed             # + vector embeddings

# Live watch
codespine watch --path .                     # file-save-triggered direct-to-graph writes
codespine watch --path . --install-hook      # also install post-commit git hook
codespine watch --path . --uninstall-hook    # remove git hook

# Search & Analysis (CLI)
codespine search "query"                     # hybrid search
codespine context "symbol"                   # one-shot deep context
codespine impact "symbol"                    # caller-tree impact (includes DI consumers)
codespine deadcode                           # dead code candidates
codespine flow                               # execution flows
codespine community                          # architectural clusters
codespine coupling                           # git change coupling
codespine diff main..feature                 # symbol-level branch diff

# Status & Info
codespine stats                              # per-project stats (--shards for shard layout)
codespine list                               # indexed projects
codespine status                             # service and database status
codespine guide                              # tool catalog and workflows

# Overlay
codespine overlay-status                     # dirty overlay state
codespine overlay-promote                    # commit overlay to base
codespine overlay-clear                      # discard overlay

# Server Management
codespine start                              # launch background MCP server (daemon)
codespine stop                               # stop background MCP server
codespine mcp                                # foreground MCP (stdio, for IDE clients)

# Model & Setup
codespine install-model                      # download embedding model for semantic search
codespine setup                              # check dependencies

# Cleanup & Reset
codespine clear-project <project_id>         # remove one project
codespine clear-index                        # remove all indexed data
codespine force-reset                        # emergency: delete all data files
```

`analyse` defaults to incremental mode. Repeat runs only process changed files and are fast.

Deep analysis (`--deep`) now runs automatically for repos with ≤ 3,000 files. For larger repos, pass `--deep` explicitly. Use `--incremental-deep` when you want a fast file-only update but still want communities, flows, dead code, and coupling refreshed.

---

## Workspace and Module Detection

CodeSpine can index:

- A single Java repo
- A multi-module Maven or Gradle project
- A workspace directory containing multiple independent repos

**Project IDs:**

| Layout | Project ID |
|--------|------------|
| Single-module repo | `payments-service` |
| Multi-module repo: core | `payments-service::core` |
| Multi-module repo: api  | `payments-service::api` |

Pass the same project ID to any MCP tool or CLI command that accepts `project=` to scope results.

---

## DI / Injection Analysis

CodeSpine resolves dependency injection bindings at index time and stores them as first-class graph edges.

**What is indexed:**

| Annotation | Edge |
|------------|------|
| `@Inject` / `@Autowired` field | `INJECTS(consumer → provider, confidence=0.85)` |
| `@Provides` / `@Bean` method | `INJECTS(config_class → return_type, confidence=0.90)` |
| `@Component` / `@Service` impl | `BINDS_INTERFACE(impl → interface, confidence=0.95)` |

**Impact on existing tools:**

- `get_impact("PaymentService")` includes classes that inject `PaymentService`, not just direct callers.
- `detect_dead_code` skips classes that are referenced only via DI edges.

**Dedicated tool:**

```python
find_injections("PaymentProcessor")
# → @Inject/@Autowired consumers
# → @Bean/@Provides providers
# → @Component/@Service implementations
```

---

## Instant Change Visibility

CodeSpine writes file changes directly to the graph — no O(N) overlay merge on every query.

When `codespine watch` detects a file save:

1. Parses the changed file with tree-sitter
2. Atomically clears and re-writes that file's methods, calls, and type relationships
3. Snapshots the write DB to the read replica
4. The MCP server picks up the new snapshot on its next tool call

Every tool — `search_hybrid`, `get_impact`, `get_symbol_context`, `find_injections` — reflects the latest file save within the debounce window (default 1–2 s).

### Git Commit Auto Re-index

Watch mode polls `git HEAD` every 5 s. When HEAD changes it runs `git diff --name-only` to find the modified Java files and re-indexes only those — not the full project.

Install an optional post-commit hook so re-indexing fires immediately on every commit:

```bash
codespine watch --path . --install-hook
```

Or from MCP:

```python
start_watch(path=".", install_hook=True)
```

The hook is idempotent and can be removed:

```bash
codespine watch --uninstall-hook --path .
```

---

## Sharding (Multi-Shard Storage)

For large workspaces with many independent projects, CodeSpine distributes project data across multiple on-disk KùzuDB shards using a consistent hash ring.

**Default:** 4 shards stored under `~/.codespine/shards/{0,1,2,3}/db`.

**Key property — module co-location:** All modules of the same project always land on the same shard so cross-module call resolution stays local. `myapp::core` and `myapp::api` always share one shard.

**Parallel indexing:** Projects on different shards are indexed concurrently; modules on the same shard are indexed serially to avoid write contention.

**Configuration:**

```bash
# Override shard count (applied at first use; changing later requires re-index)
export CODESPINE_SHARDS=8
codespine analyse /path/to/project
```

**Shard topology:**

```bash
codespine stats --shards
```

**Programmatic access:**

```python
from codespine.sharding.store import ShardedGraphStore

sg = ShardedGraphStore(num_shards=4)
store = sg.shard("my-project")           # returns the right GraphStore shard
projects = sg.list_project_metadata()    # fan-out across all shards
```

**Migration from v0.9.x:** On first run after upgrading, `~/.codespine_db` is automatically migrated to shard 0's path (`~/.codespine/shards/0/db`). No manual steps required.

---

## Storage Backends

CodeSpine ships two storage backends.  **DuckDB is the default** starting with v1.0.0.  KùzuDB is retained as the alternate for users who need its property-graph Cypher interface.

### DuckDB (default)

- 10–50× faster batch writes (`executemany` on flat relational tables vs. Kuzu's property-graph MERGE/UNWIND).
- Single-file database — snapshots are a plain file copy after `CHECKPOINT`.
- Standard SQL for direct inspection with any DuckDB client or notebook.
- Transparent Cypher→SQL translation: all analysis modules continue to issue Cypher queries internally; the DuckDB adapter translates them automatically.
- Bundled in `codespine`'s core dependencies — no extra install step.

### KùzuDB (alternate)

Native property-graph with Cypher.  Prefer this when you need the `run_cypher` MCP tool for ad-hoc traversals or when integrating with other Kuzu tooling.

**Switch to KùzuDB:**

```bash
CODESPINE_BACKEND=kuzu codespine analyse /path/to/project
CODESPINE_BACKEND=kuzu codespine mcp
```

**Per-instance:**

```python
from codespine.sharding.store import ShardedGraphStore

sg = ShardedGraphStore(backend="kuzu", num_shards=4)    # KùzuDB
sg = ShardedGraphStore(backend="duckdb", num_shards=4)  # DuckDB (default)
```

> **Note:** keep `CODESPINE_BACKEND` consistent between the indexer and MCP server for the same shard path — mixing backends on the same path will produce errors.

---

## Result Caching

Expensive analysis tools cache their results for 5 minutes. The cache is keyed by `(tool_name, arguments, snapshot_mtime)` so a new index snapshot automatically invalidates stale entries.

**Cached tools:** `get_impact`, `detect_dead_code`.

The cache is per MCP server instance (in-memory, not persisted across restarts). It is invalidated automatically when `reindex_file` or `analyse_project` completes.

**Cache stats** are visible via `get_capabilities()`.

---

## Deep Analysis Details

The deep analysis phase covers four passes that are expensive but optional:

| Pass | What it does | When to use |
|------|-------------|-------------|
| Communities | Detects structural clusters (Leiden algorithm) | Architectural exploration, community tools |
| Execution flows | Traces call paths from public entry points | `trace_execution_flows`, `get_symbol_context` |
| Dead code | Finds methods with no callers (Java-aware exemptions) | Cleanup audits |
| Change coupling | Analyses git history for co-changed file pairs | `get_change_coupling`, `related` |

**Auto-threshold:** deep analysis runs automatically when the project has ≤ 3,000 Java files. Larger repos get lightweight flow/dead-code passes; full deep analysis requires `--deep`.

**Incremental deep:** `--incremental-deep` combines incremental file indexing with a forced full deep pass — useful after large refactors where you want the call graph refreshed quickly but also want updated communities and coupling.

```bash
codespine analyse . --incremental-deep
```

**Embeddings** (`--embed`) are independent of deep analysis. Without them, BM25 + fuzzy search still works. Add embeddings when you need concept-level retrieval ("find retry logic", "find payment processing").

---

## Concurrent Indexing and Querying

The indexer (write) and the MCP daemon (read) use separate database paths and buffer pools:

| Path | Pool | Purpose |
|------|------|---------|
| `~/.codespine/shards/{N}/db` | 512 MB | Indexer write path |
| `~/.codespine/shards/{N}/db_read` | 128 MB | MCP + CLI read path |

When indexing completes, the write DB is atomically snapshotted to the read path and a sentinel file is touched. The MCP daemon detects the sentinel change and silently reloads from the new snapshot on the next tool call — no restart needed.

Running `codespine analyse --deep --embed` on one project while querying a different one no longer causes buffer pool OOM or lock contention.

---

## Runtime Files

```
~/.codespine/
  shards/
    0/
      db/          # Shard 0 write database (KùzuDB directory or DuckDB .db file)
      db_read/     # Shard 0 read replica
      db_read.updated  # Sentinel; touched after each snapshot
    1/ …           # Shards 1-3 (same layout)

~/.codespine.pid               # Background MCP server PID
~/.codespine.log               # Background server log
~/.codespine_embedding_cache.json  # Embedding cache (thread-safe JSON)
~/.codespine_index_meta/       # Incremental file metadata (SHA hashes)
~/.codespine_overlay/          # Legacy overlay directory (direct-to-graph is primary)

# Legacy paths (pre-0.9.7; auto-migrated to shards/0/ on first run)
~/.codespine_db/
~/.codespine_db_read/
```

---

## Programmatic API

```python
from codespine.sharding.store import ShardedGraphStore
from codespine.indexer.engine import JavaIndexer
from codespine.analysis.impact import analyze_impact
from codespine.search.hybrid import hybrid_search

# Open (or create) the store
sg = ShardedGraphStore()
store = sg.shard("my-project")

# Index a project
result = JavaIndexer(store).index_project("/path/to/project", full=True, project_id="my-project")
print(f"Indexed {result.files_indexed} files, {result.methods_indexed} methods")

# Snapshot so readers see the new data
store.snapshot_to_read_replica()

# Search
hits = hybrid_search(store, "payment processor", project="my-project")

# Impact analysis
impact = analyze_impact(store, "PaymentService", max_depth=4, project="my-project")
```

---

## Notes

- `codespine start` launches a background MCP server. Most IDE MCP clients should use `codespine mcp` instead and manage the process themselves.
- `codespine watch` writes changes directly to the graph and snapshots the read replica after each batch. MCP queries reflect file saves within the debounce window.
- `git HEAD` is polled every 5 s. On a new commit, only the changed Java files are re-indexed via `git diff --name-only` — not the full project.
- `codespine clear-index` rebuilds the local databases from scratch. This also removes the read replicas; run `analyse` again to republish.
- `codespine force-reset` is the nuclear option — it deletes all data files without going through the DB engine. Use it when `clear-index` fails due to DB corruption (e.g. after an abrupt Ctrl+C mid-write with KùzuDB).
- For large Spring or JPA-heavy repos, dead-code results should be reviewed before deletion. The tool is conservative by default; use `strict=True` for a more aggressive audit.
- The `CODESPINE_BACKEND` env var must be set consistently across the indexer and the MCP server — mixing backends on the same shard path will produce errors.

---

## Project Links

- [GitHub](https://github.com/vinayak3022/codeSpine)
- [Issues](https://github.com/vinayak3022/codeSpine/issues)
- [PyPI](https://pypi.org/project/codespine/)
- [Contributing](.github/CONTRIBUTING.md)
- [Security](.github/SECURITY.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
