# CodeSpine

**v1.0.15** — Local Java code intelligence for coding agents, backed by a graph database.

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

Default install includes the CLI, MCP server, Java indexer, watch mode, health checks, background task tracking, and graph/search commands.

Optional semantic search (sentence-transformers):

```bash
pip install "codespine[ml]"
```

Add the local index explorer UI:

```bash
pip install "codespine[ui]"
```

The current lite UI is dependency-free and served locally by CodeSpine; the `ui` extra is the stable add-on install target for the browser explorer.
Quotes are required in zsh: use `pip install "codespine[ui]"`, not `pip install codespine[ui]`.

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
codespine analyse /path/to/java-project --complete --deep

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
Index self-test...             passed
Index health...                no anomalies

Done in 4.2s — 623 symbols, 1,847 edges, 8 clusters, 34 flows
Publishing read replica...     MCP will reload automatically
```

Each analysis phase streams live progress. The final step publishes a read replica so the MCP daemon picks up the new index without restarting.

---

## GraphRAG Answer Surface

CodeSpine includes a full graph-augmented generation (`answer`) tool that synthesizes indexed graph data into grounded, citable answers — no external LLM required for the retrieval/reranking pipeline.

When an agent calls `answer(question, project=...)`, the system:

1. **Resolves the focus symbol** via hybrid search (BM25 + vector + fuzzy) scoped to the project.
2. **Builds deep context** — impact caller tree, community cluster, and execution flows.
3. **Assembles evidence candidates** from search results, impact callers, community matches, and flow entries — each with a quality-adjusted `rerank_score`.
4. **Applies graph-aware diversity reranking** — a greedy MMR-style selection that prefers underrepresented evidence kinds, producing a broader evidence mix than pure utility ranking.
5. **Generates an evidence subgraph** linking the focus symbol to each selected evidence item with typed edges.
6. **Computes confidence** from evidence count, kind diversity, and impact depth.
7. **Returns citations** mapping every evidence item back to its source (hybrid_search, analyze_impact, symbol_community, or trace_execution_flows).

### Safe Abstention

The system **refuses to guess** when it cannot ground an answer:

| Abstention trigger | Behaviour |
|-------------------|-----------|
| **Ambiguous focus** — multiple symbols match the query closely (lexical similarity + score proximity) | Returns `abstained=True` with the ambiguity details and recommended disambiguation tools (`find_symbol`, `search_hybrid`) |
| **No focus found** — hybrid search returned zero matches | Returns `abstained=True` with `note` explaining no symbol was matched |
| **Insufficient evidence** — focus resolved but no evidence candidates passed the quality threshold | Returns `abstained=True` with `note` explaining the grounding failure |

In all three cases the response includes:
- `answer_contract.status = "abstained"`
- `fallback.recommended_tools` — actionable suggestions for the agent
- Full `provenance` and `observability` metadata (same shape as a supported answer)

### Evidence Reranking

Each evidence candidate receives a `rerank_score` based on:

| Evidence kind | Base weight | Additional signals |
|--------------|-------------|-------------------|
| `search_result` | 2.7 | BM25/vector/fuzzy score, confidence label, exact focus-anchor match (+1.55) |
| `impact` | 3.0 | Call depth (direct=+0.35, indirect=+0.2, transitive=+0.1), edge confidence |
| `community` | 2.2 | Community cohesion (+0.25 per point), **high-cohesion bonus** (+0.15 if cohesion > 0.7) |
| `flow` | 2.0 | Flow depth (entry level=+0.25, deeper=+0.05 to +0.2) |

The **graph-aware diversity reranker** (Tranche 9) then applies a greedy diverse-selection pass: each evidence kind already represented in the selected set receives a diversity penalty (`1.0 - 0.3 × kind_ratio`), encouraging the system to pick from under-represented kinds. This produces answers with broader architectural coverage.

### Answer Caching

GraphRAG answers are cached in-memory for 5 minutes (TTL, max 128 entries). Cache keys include:

- **Provenance version** — cache invalidates automatically when the response contract changes
- **Question text** and **project scope**
- **Snapshot mtime** — base index timestamp
- **Overlay mtime** — active overlay/dirty-file timestamp

A change to either the base snapshot or the overlay immediately produces a cache miss and a fresh answer. Each cached response includes `observability.cache.hit` and the mtime values that produced it.

### Latency Observability

Every answer includes `observability.latency_ms` with per-stage breakdown:
- `context` — `build_symbol_context()` timings (search + impact + community + flows)
- `evidence_build` — evidence candidate assembly and reranking
- `cache_lookup` — cache read time
- `serialization` — JSON serialization
- `total` — wall-clock elapsed time

---

## Benchmark Results (v1.0.15 smoke benchmark)

The numbers below were measured on **commit `e2e4145`** using an **Apple M2 / macOS 25.4.0 / Python 3.10.9 / DuckDB backend** with **no ML model installed** (hash-embedding fallback). The benchmark corpus was the bundled Java fixture copied to a temporary workspace:

- dataset: `tests/fixtures/java_simple`
- size: **3 files / 3 classes / 4 methods / 3 call edges / 7 symbols**
- GraphRAG evaluation suite: `benchmarks/java_simple_answer_eval.json`

This is a **smoke benchmark**, not a scale benchmark. It is useful for regression detection, cache validation, and end-to-end correctness, but it is too small to represent medium/large Java repos.

### Methodology

- **Indexing:** 5 fresh runs of `analyse --complete --deep`
- **Retrieval latency:** 15 in-process runs each for `search_hybrid(..., explain=True)` and `analyze_impact(...)`
- **GraphRAG latency:** 10 cold/warm pairs using the same in-process store to measure cache behavior
- **Quality:** `answer-eval` against the bundled suite
- **Freshness:** modify one Java file, rerun incremental `analyse`, verify the new symbol is searchable in a fresh process

### Indexing

| Benchmark | Runs | Median | P95 | Max |
|-----------|------|--------|-----|-----|
| `analyse --complete --deep` | 5 | **1125.51 ms** | **1695.40 ms** | 1831.70 ms |
| Incremental `analyse` after one file change | 1 | **1117.07 ms** | — | — |

### Retrieval quality

Measured on 5 labeled queries (`processPayment`, `helper`, `App`, `ServiceTest`, `process payment`) against the temporary copied fixture corpus (`project_id=query_corpus`):

| Metric | Value |
|--------|-------|
| Top-1 accuracy | **1.00** |
| Recall@3 | **1.00** |
| MRR@3 | **1.00** |

### Query latency

| Tool | Runs | Median | P95 | Max |
|------|------|--------|-----|-----|
| `search_hybrid(..., explain=True)` | 15 | **5.68 ms** | **5.88 ms** | 5.89 ms |
| `analyze_impact(...)` | 15 | **9.59 ms** | **10.42 ms** | 11.40 ms |
| `graph_rag_answer(...)` cold | 10 | **22.81 ms** | **23.33 ms** | 23.34 ms |
| `graph_rag_answer(...)` warm | 10 | **0.08 ms** | **0.10 ms** | 0.10 ms |

Observed GraphRAG warm-cache hit rate in-process: **100%**.

### GraphRAG quality on the tiny corpus

`answer-eval` against `benchmarks/java_simple_answer_eval.json` produced:

| Metric | Value |
|--------|-------|
| Average regression score | **70.0** |
| Min case score | **70.0** |
| Pass rate | **1.0** |
| Quality gates passed | **true** |

For this smoke suite, `min_average_score` is intentionally set to **70.0** because the corpus is designed to validate **correct abstention behavior** on ambiguous tiny-project queries rather than multi-evidence supported answers.

Important caveat: on this tiny corpus, the current GraphRAG answer surface **abstains** on semantically meaningful questions such as `what breaks if I change processPayment?` because the ambiguity threshold treats nearby class/method names as unsafe to guess between. That is why the measured GraphRAG status during the latency benchmark was **`abstained`**, and why the suite is primarily an abstention/fallback benchmark rather than a supported-answer benchmark.

### Freshness / incremental correctness

After adding a new method (`newlyAddedBenchmarkMethod`) to `Service.java` and rerunning incremental `analyse`, a fresh process resolved the new method as the **top search result**:

- top result: `com.example.Service#newlyAddedBenchmarkMethod()`
- visibility after incremental reindex: **true**

### What this benchmark says today

- **Indexing and read-path latency are fast** on the bundled smoke corpus.
- **GraphRAG caching works**: warm calls dropped from ~22.8 ms median to ~0.08 ms median in-process.
- **Incremental freshness works** when verified from a fresh process after re-index.
- **GraphRAG quality on tiny corpora is conservative**: abstention is correct and stable, but the current ambiguity policy is strict enough to fail the default `min_average_score=80` gate on the bundled smoke suite.

For a production benchmark on real codebases, run the same workflow on a medium and large Java corpus and keep the same reporting fields (dataset size, cold/warm split, p95, retrieval accuracy, and `answer-eval` score).

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

## MCP Tools (46 total)

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
| `search_hybrid(query, k, project, explain)` | Ranked symbol search (BM25 + vector + fuzzy via RRF) with `high/medium/low` confidence scores; `explain=True` adds versioned provenance, index fingerprint, per-ranker traces, match reasons, confidence explanations, and a retrieval contract (`candidate_pool_size`, `returned`, `supports_rerank`). |
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
| `answer(question, project)` | **GraphRAG answer surface.** Resolves the focus symbol, builds deep context (impact + community + flows), applies graph-aware diverse evidence reranking, returns evidence subgraph with typed edges, per-item citations, confidence score, per-stage latency, provenance/envelope, index fingerprint, and safe abstention on ambiguity or weak grounding. Answers are cached with overlay-aware invalidation. |
| `ask(question, project)` | Keyword-based natural language dispatcher: routes "who calls X", "what breaks if Y", "explain Z", "find methods named …" to the right tool automatically. |
| `what_breaks(symbol, project)` | Plain-English blast-radius summary with `risk_level` (low / medium / high). |
| `explain(symbol, project)` | What a class or method does and how it fits in the architecture. |
| `read_symbols(file_path, symbols)` | Extract only the requested method source ranges from a file using tree-sitter instead of reading the whole file. |
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
codespine analyse <path>                     # trust-first incremental index (default)
codespine analyse <path> --full              # full re-index from scratch
codespine analyse <path> --fast --budget 90  # budgeted partial core index
codespine analyse <path> --complete --deep   # + communities, flows, dead code, coupling
codespine analyse <path> --complete --incremental-deep
codespine analyse <path> --embed             # + vector embeddings
codespine repair <project-id-or-path>        # retry failed or incomplete phase
codespine repair <project-id-or-path> --full # force full core rebuild

# Background jobs and local UI
codespine background                         # background task progress
codespine tasks                              # running/recent background work
codespine ui                                 # local read-only index explorer
codespine ui --open                          # open http://127.0.0.1:8765

# Live watch
codespine watch --path .                     # file-save-triggered direct-to-graph re-indexing

# Search & Analysis (CLI)
codespine answer "question" --project app                                         # GraphRAG answer (evidence subgraph, citations, confidence, provenance)
codespine answer-eval --suite suite.json --project app                            # GraphRAG regression scoring + quality gates
codespine answer-eval --suite suite.json --project app --json                     # structured JSON output for CI
codespine answer-eval --suite suite.json --project app --min-average-score 90      # override suite gates
codespine answer-eval --suite suite.json --project app --min-case-score 70         # per-case minimum
codespine answer-eval --suite suite.json --project app --min-pass-rate 1.0         # strict pass rate
codespine answer-eval --suite suite.json --project app --max-depth 4 --k 3         # override retrieval params
codespine search "query" --project app       # scoped hybrid search
codespine search "query" --explain           # provenance-aware hybrid search with index fingerprint
codespine context "symbol"                   # one-shot deep context
codespine impact "symbol"                    # caller-tree impact (includes DI consumers)
codespine deadcode                           # dead code candidates
codespine flow                               # execution flows
codespine community                          # architectural clusters
codespine coupling                           # git change coupling
codespine diff main..feature                 # symbol-level branch diff

# Status & Info
codespine stats                              # per-project stats (--shards for shard layout)
codespine health                             # index coverage and anomaly dashboard
codespine self-test                          # smoke queries for schema/translator checks
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

`analyse` is trust-first by default: it completes the core graph in the foreground, validates and publishes the read replica, then keeps deep enrichment moving in the background. Use `--fast` only when you want a budgeted partial core index. Use `codespine background`, `codespine ui`, or `codespine repair` to inspect and recover incomplete or degraded work.

GraphRAG regression suites are JSON objects with `cases` and optional `gates` thresholds. Each case can assert availability, abstention, focus, evidence kinds, citations, confidence, term inclusion/exclusion, and minimum scores. The scorer runs 10+ weighted checks and produces a structured report with per-check pass/fail, deltas, and observed values. Quality gates enforce `min_average_score`, `min_case_score`, and `min_pass_rate` — suite-defined gates take precedence over CLI defaults.

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

Install an optional post-commit hook from MCP so re-indexing fires immediately on every commit:

```python
start_watch(path=".", install_hook=True)
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

## Provenance & Traceability

Every GraphRAG answer and hybrid search explain response includes a **versioned provenance envelope** with full trace metadata for audit, debugging, and regression analysis.

### Response Metadata

```json
{
  "provenance": {
    "version": 10,
    "package_version": "1.0.15",
    "retrieval_mode": "graph_rag",
    "question": "what breaks if I change PaymentService?",
    "project": "app",
    "focus_id": "com.example.PaymentService#processPayment",
    "candidate_counts": {
      "search_result": 3,
      "impact": 5,
      "community": 2,
      "flow": 1
    },
    "search_candidate_count": 3,
    "evidence_sources": ["hybrid_search", "analyze_impact", "symbol_community"],
    "context_timings_ms": {
      "search": 12,
      "impact": 45,
      "community": 8,
      "flows": 3,
      "total": 68
    },
    "index_fingerprint": {
      "snapshot_mtime": 1712345678.123,
      "overlay_mtime": 0.0
    }
  }
}
```

### What each field tells you

| Field | Purpose |
|-------|---------|
| `version` | Provenance schema version; incremented on breaking changes. Cache keys include this field for automatic invalidation. |
| `package_version` | The CodeSpine version that generated this response. Ties answers to a specific release. |
| `index_fingerprint` | Snapshot and overlay mtimes at the time the answer was generated. Enables deterministic replay: same fingerprint + same query → same answer (modulo caching). |
| `candidate_counts` | How many candidates of each evidence kind were available before reranking. |
| `context_timings_ms` | Per-stage latency for context assembly (search, impact, community, flows). |
| `evidence_sources` | Unique set of retrieval backends that supplied selected evidence. |

The same provenance envelope is exposed in two places:
- **Top-level `provenance`** — for direct consumer access
- **`observability.provenance`** — for monitoring and observability pipelines

### Hybrid Search Provenance

When `search_hybrid(..., explain=True)` is used, the response includes a parallel provenance structure with the same `version`, `package_version`, `index_fingerprint`, and per-ranker traces:

```json
{
  "retrieval_contract": {
    "version": 10,
    "fusion": "rrf",
    "rankers": ["bm25", "semantic", "fuzzy"],
    "candidate_pool_size": 142
  },
  "provenance": {
    "version": 10,
    "package_version": "1.0.15",
    "candidate_pool_size": 142,
    "index_fingerprint": {
      "snapshot_mtime": 1712345678.123,
      "overlay_mtime": 0.0
    },
    "rankers": {
      "bm25": {"traces": [...]},
      "semantic": {"traces": [...]},
      "fuzzy": {"traces": [...]}
    }
  }
}
```

---

## Quality Gates & Regression Testing

CodeSpine includes a **continuous evaluation framework** (Tranche 7) that scores GraphRAG answers against expected contracts and enforces quality thresholds in CI.

### Scoring

Each answer is scored against an expectation object:

```json
{
  "available": true,
  "abstained": false,
  "focus_id": "com.example.PaymentService#processPayment",
  "min_evidence_count": 2,
  "min_citation_count": 2,
  "requires_evidence_kinds": ["search_result", "impact"],
  "must_include_terms": ["Best match", "Impact"],
  "min_confidence": "medium"
}
```

The scorer runs 10+ checks covering availability, abstention, focus match, evidence count, citation count, evidence kind coverage, term inclusion/exclusion, and confidence thresholds. Each check contributes a weighted delta to the final score (0–100).

### Quality Gates

Results are validated against configurable gates:

| Gate | Default | Description |
|------|---------|-------------|
| `min_average_score` | 80.0 | Minimum average score across all cases |
| `min_case_score` | 70.0 | Minimum score for any single case |
| `min_pass_rate` | 1.0 | Fraction of cases that must pass |

Suites are JSON files with `cases` and optional `gates`:

```json
{
  "name": "payment-service-regression",
  "gates": { "min_average_score": 85.0, "min_pass_rate": 0.9 },
  "cases": [
    {"name": "processPayment-impact", "question": "what breaks if I change processPayment?", "expect": {"available": true, "focus_id": "...processPayment", "min_evidence_count": 2, "requires_evidence_kinds": ["search_result", "impact"]}},
    {"name": "unknown-symbol",        "question": "what breaks if I change NonExistent?",     "expect": {"available": false, "abstained": true}}
  ]
}
```

Suite-defined gates take precedence over CLI defaults unless explicitly overridden:

```bash
codespine answer-eval --suite suite.json --project app --min-average-score 90 --json
```

### Programmatic API

```python
from codespine.graphrag import evaluate_graph_rag_suite, score_graph_rag_answer

report = evaluate_graph_rag_suite(store, suite_payload, project="app")
report["quality_gates"]["passed"]   # → True/False
report["summary"]["average_score"]  # → float
report["summary"]["pass_rate"]      # → float
```

---

## Result Caching

Expensive analysis tools cache their results for 5 minutes. The cache is keyed by `(tool_name, arguments, snapshot_mtime)` so a new index snapshot automatically invalidates stale entries.

**Cached tools:** `get_impact`, `detect_dead_code`, `answer`.

The cache is per MCP server instance (in-memory, not persisted across restarts). It is invalidated automatically when `reindex_file` or `analyse_project` completes.

**Cache stats** are visible via `get_capabilities()`. `answer` also exposes per-stage latency timing in its observability payload and cache hit/miss state with snapshot/overlay mtimes for forensic debugging.

### GraphRAG Cache Details

The `answer` tool uses a purpose-built cache (`ResultCache` with 128-entry LRU and 300 s TTL) keyed on:

- Provenance schema version (bumped on contract changes)
- Question text and project scope
- Snapshot mtime (base index timestamp)
- Overlay mtime (active overlay/dirty-file timestamp)

This design ensures:
- An answer is never served from cache after re-indexing
- Overlay edits immediately invalidate the corresponding cached answer
- A provenance version bump invalidates all cached answers in one shot

---

## Deep Analysis Details

The deep analysis phase covers four passes that are expensive but optional:

| Pass | What it does | When to use |
|------|-------------|-------------|
| Communities | Detects structural clusters (Leiden algorithm) | Architectural exploration, community tools |
| Execution flows | Traces call paths from public entry points | `trace_execution_flows`, `get_symbol_context` |
| Dead code | Finds methods with no callers (Java-aware exemptions) | Cleanup audits |
| Change coupling | Analyses git history for co-changed file pairs | `get_change_coupling`, `related` |

**Trust-first default:** `codespine analyse` now treats success as “the core graph is complete, validated, and queryable.” The default foreground path finishes parse, DB writes, call resolution, type resolution, DI resolution, and snapshot publish before it returns. Deep enrichment still continues in the background unless you use `--complete --deep`.

**Fast mode:** `codespine analyse --fast` keeps the old budgeted behavior for large repos. If the budget expires before the core graph is complete, CodeSpine publishes a partial snapshot, marks the project as partial, and tracks the background continuation until the core graph is repaired.

**Health checks:** every analyse run now performs a small self-test query suite and reports index anomalies such as large projects with zero call edges, plus graph integrity checks for dangling files/classes/methods/symbols. Use `codespine health` for the terminal dashboard or `codespine self-test --json` in CI.

**Background visibility:** `codespine background` shows status, result, last phase, progress, and repair hints in the terminal, and `codespine tasks` remains available as the shorter registry view. `codespine ui` serves a local explorer with project state (`ready`, `enriching`, `partial`, `degraded`, `repair_required`), background tasks, and one-click Repair/Reindex actions at `http://127.0.0.1:8765`.

**Repair flows:** `codespine repair <project-id-or-path>` retries the failed or incomplete phase first. Use `--full` when the project needs a full core rebuild.

**Complete deep:** `--complete --deep` runs the expensive enrichment passes before returning. `--complete --incremental-deep` combines incremental file indexing with a forced full deep pass.

```bash
codespine analyse . --complete --incremental-deep
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
from codespine.graphrag import graph_rag_answer, evaluate_graph_rag_suite, score_graph_rag_answer

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

# GraphRAG answer with full provenance envelope
answer = graph_rag_answer(store, "what breaks if I change PaymentService?", project="my-project")
print(answer["answer"])                           # → "Best match: … Impact: … Evidence: …"
print(answer["provenance"]["version"])             # → 10
print(answer["provenance"]["index_fingerprint"])   # → { snapshot_mtime: …, overlay_mtime: … }
print(answer["observability"]["latency_ms"])       # → per-stage timings
print(answer["observability"]["cache"]["hit"])     # → True/False

# Score an answer against an expected contract
report = score_graph_rag_answer(answer, {"available": True, "min_evidence_count": 2})
print(report["passed"], report["score"])           # → True, 92.5

# Run a full regression suite with quality gates
suite = {
    "name": "payment-regression",
    "gates": {"min_average_score": 85.0},
    "cases": [
        {"question": "what breaks if I change PaymentService?", "expect": {"available": True, "min_evidence_count": 2}},
        {"question": "unknown symbol", "expect": {"available": False, "abstained": True}},
    ],
}
eval_report = evaluate_graph_rag_suite(store, suite, project="my-project")
print(eval_report["quality_gates"]["passed"])      # → True/False
print(eval_report["summary"]["average_score"])     # → float
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
- **GraphRAG answers** are never served from cache after re-indexing or overlay edits. Cache keys include both snapshot and overlay mtimes plus the provenance schema version.
- **Quality gate failures** in CI produce a non-zero exit code and a detailed JSON report listing every failed check per case. Use `--json` for structured CI integration.
- **Abstained answers** still return full provenance and observability metadata, so CI pipelines can distinguish "system refused to guess" from "system failed".
- **Evidence diversity** may reorder results compared to earlier versions: the graph-aware diverse reranker prefers underrepresented evidence kinds over pure utility ranking. This produces broader architectural coverage at the cost of different top-k ordering.

---

## Project Links

- [GitHub](https://github.com/vinayak3022/codeSpine)
- [Issues](https://github.com/vinayak3022/codeSpine/issues)
- [PyPI](https://pypi.org/project/codespine/)
- [Contributing](.github/CONTRIBUTING.md)
- [Security](.github/SECURITY.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
