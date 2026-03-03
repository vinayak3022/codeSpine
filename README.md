# CodeSpine

**A code-intelligence layer for Java codebases — purpose-built for AI agents.**

Instead of making your agent read hundreds of raw source files, CodeSpine maps your entire codebase into a live graph and exposes it through 24 structured MCP tools.
Your agent asks a question, gets a precise answer — no file trawling, no wasted tokens, no hallucinated call chains.

> **Token efficiency in practice**: a `get_symbol_context` call returns a fully-resolved call graph for a symbol in one round-trip.
> The equivalent "read every relevant file" approach typically costs 10-50× more tokens and still misses transitive edges.

---

## How it works

```
Your Java codebase
       │
  codespine analyse          ← one-time (or on-demand) indexing
       │
  ~/.codespine_db            ← Kuzu graph DB (symbols, calls, communities, flows …)
       │
  codespine mcp              ← FastMCP server — 24 tools
       │
  Your AI agent (Claude, GPT, Cursor, Cline …)
```

Agents talk to the MCP server. They never need to open a `.java` file unless they are actually editing it.

---

## Installation

```bash
pip install codespine
```

Optional: install `sentence-transformers` to enable semantic vector search (adds ~500 MB of model weight).

```bash
pip install sentence-transformers
```

---

## Quick Start

### 1 — Index your codebase

```bash
# Fast (BM25 + fuzzy search, no embeddings — recommended first run)
codespine analyse /path/to/your/project

# Full (adds semantic vector search, takes longer)
codespine analyse /path/to/your/project --embed

# Deep (+ dead code, execution flows, communities, git coupling)
codespine analyse /path/to/your/project --deep
```

Example output:

```
$ codespine analyse .
Walking files...               142 files found
Parsing code...                142/142  (parallel, 4 workers)
Tracing calls...               847 calls resolved
Analyzing types...             234 type relationships
Detecting communities...       8 clusters found
Detecting execution flows...   34 processes found
Finding dead code...           12 unreachable symbols
Analyzing git history...       18 coupled file pairs

Done in 18s — 623 symbols, 1 847 edges, 8 clusters, 34 flows
```

### 2 — Wire up MCP

Add to your MCP config (`~/.claude/mcp.json` or equivalent):

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

### 3 — Let the agent explore

The agent can now call tools like:

```
search_hybrid("payment retry logic")
get_symbol_context("processPayment")
get_impact("com.example.PaymentService#charge")
detect_dead_code()
get_codebase_stats()
```

---

## MCP Tools (24)

### Connectivity & Discovery

| Tool | What it does |
|------|-------------|
| `ping()` | Confirm the server is alive. Call this first. |
| `get_capabilities()` | Returns what is indexed right now — projects, symbol counts, which features are available, and whether watch mode is running. Call before other tools to avoid trial-and-error. |
| `list_projects()` | List every indexed project with path, symbol count, and file count. |
| `get_codebase_stats()` | Per-project breakdown: files, classes, methods, calls, embeddings, totals. |

### Search

| Tool | What it does |
|------|-------------|
| `search_hybrid(query, k, project)` | BM25 + semantic vector + fuzzy, fused with RRF. Scope to a project with `project=`. |
| `find_symbol(name, kind, project, limit)` | Exact / prefix name lookup returning **all** matches grouped by project. Use this when the same class name exists in multiple projects to pick the right one. |
| `list_packages(project, limit)` | All Java packages with class count, grouped by project. Good for structural orientation before searching. |

### Analysis

| Tool | What it does |
|------|-------------|
| `get_symbol_context(query, max_depth, project)` | Full call graph context for a symbol — callers, callees, types, up to `max_depth` hops. |
| `get_impact(symbol, max_depth, project)` | Depth-grouped impact analysis with confidence scores. Shows what breaks if this symbol changes. |
| `detect_dead_code(limit, project)` | Unreachable symbols after applying framework exemptions (Spring, JPA, …). |
| `trace_execution_flows(entry_symbol, max_depth, project)` | Execution paths from entry points (or a specific symbol). |
| `get_symbol_community(symbol)` | Which community cluster a symbol belongs to, with co-members. |
| `get_change_coupling(months, min_strength, min_cochanges, project)` | Git-derived file pairs that change together — useful for predicting collateral changes. |

### Git

| Tool | What it does |
|------|-------------|
| `git_log(file_path, limit, project)` | Commit history for a file or the whole repo. |
| `git_diff(ref, file_path, project)` | Diff against a ref (default `HEAD`). |
| `compare_branches(base_ref, head_ref)` | Symbol-level diff between two branches — which classes/methods changed. |

### Watch Mode (live incremental reindex)

| Tool | What it does |
|------|-------------|
| `start_watch(path, global_interval)` | Start incremental reindexing in the background. Watches for file changes and updates the graph within `global_interval` seconds. **Recommended**: keep this running during active development sessions so the graph stays fresh. |
| `stop_watch()` | Gracefully stop the background watcher. |
| `get_watch_status()` | Check if watch is running — uptime, path, interval. |

### On-demand Analysis (non-blocking)

| Tool | What it does |
|------|-------------|
| `analyse_project(path, full, deep, embed)` | Trigger a full re-analysis as a background job. Returns immediately. Poll `get_analyse_status()` for progress. |
| `get_analyse_status()` | Check background analysis — running / done / failed, last log lines. |

### Index Management

| Tool | What it does |
|------|-------------|
| `reset_project(project_id)` | Delete all graph data for one project (clean-slate re-index). |
| `reset_index()` | Wipe the entire index — all projects, communities, flows. |

### Power / Debug

| Tool | What it does |
|------|-------------|
| `run_cypher(query)` | Execute a raw Cypher read query against the graph (Kuzu dialect). For advanced exploration. |

---

## CLI Reference

### Indexing

```bash
codespine analyse <path>              # fast index (no embeddings)
codespine analyse <path> --embed      # + semantic vectors
codespine analyse <path> --full       # force full re-index (skip incremental)
codespine analyse <path> --deep       # + dead code, flows, communities, git coupling
codespine analyse <path> --deep --embed  # everything
```

### Search & Analysis

```bash
codespine search "payment retry bug" [--k 20] [--json]
codespine context "processPayment"   [--max-depth 3] [--json]
codespine impact  "com.example.Service#processPayment(java.lang.String)" [--max-depth 4] [--json]
codespine deadcode [--limit 200] [--json]
codespine flow   [--entry <symbol>] [--max-depth 6] [--json]
codespine community [--symbol <symbol>] [--json]
codespine coupling  [--months 6] [--min-strength 0.3] [--min-cochanges 3] [--json]
codespine diff <base>..<head> [--json]
```

### Stats

```bash
codespine stats           # per-project table: files, classes, methods, calls, embeddings
codespine stats --json    # machine-readable output
```

### Watch

```bash
codespine watch [--path .] [--global-interval 30]
```

### Index Management

```bash
codespine clear-project <project_id>   # remove one project from the graph
codespine clear-index                  # wipe the entire index
```

---

## Workspace / Multi-Project Support

CodeSpine understands three levels of hierarchy:

```
~/IdeaProjects/              ← workspace  (a folder of independent projects)
├── payments-service/        ← project    (has its own .git / pom.xml)
│   ├── core/                ← module     (Maven <module> or Gradle subproject)
│   └── api/                 ← module
└── inventory-service/       ← project
    └── (single-module)
```

- **Workspace detection**: if the path you give to `analyse` has no `.git` or build file at its root, CodeSpine scans one level down for sub-projects and indexes them all.
- **Project IDs**: single-module → `payments-service`; multi-module → `payments-service::core`, `payments-service::api`.
- **Scoped queries**: every analysis and search tool accepts an optional `project=` parameter so agents can work within one project without noise from others.
- **Cross-project search**: omit `project=` to search across everything.

---

## Embedding / Speed Trade-off

| Flag | Index time | Search modes available |
|------|-----------|----------------------|
| *(no flag)* | Fast (~seconds–minutes) | BM25, fuzzy, exact |
| `--embed` | Slower (minutes, depends on model) | BM25, fuzzy, exact + **semantic vector** |

`sentence-transformers` must be installed for `--embed` to have any effect.
If it is not installed, indexing always skips embeddings silently.

Most agent workflows work great without embeddings — BM25 + fuzzy covers keyword, partial-name, and typo-tolerant search. Add `--embed` when you need concept-level similarity ("find all classes related to retry logic").

---

## Runtime Paths

| Path | Purpose |
|------|---------|
| `~/.codespine_db` | Kuzu graph database |
| `~/.codespine.pid` | Watch-mode PID file |
| `~/.codespine.log` | Watch-mode log |
| `~/.codespine_embedding_cache.sqlite3` | Embedding vector cache |

---

## Project Docs

- [Contributing](.github/CONTRIBUTING.md)
- [Security](.github/SECURITY.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
