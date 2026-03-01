# CodeSpine

CodeSpine is a Java code intelligence graph for coding agents.

It indexes Java symbols and call relationships into Kuzu, runs hybrid retrieval
(BM25 + semantic + fuzzy), and exposes CLI and MCP tools for impact analysis,
dead code detection, execution flow tracing, community detection, coupling, watch
mode, and branch-level symbol diffs.

## Highlights

- Hybrid search with Reciprocal Rank Fusion
- Impact analysis with depth grouping and confidence scoring
- Java-aware dead code detection passes
- Execution flow tracing from framework-agnostic entry points
- Community detection (Leiden when available; heuristic fallback)
- Git change coupling integrated into analysis
- Watch mode with incremental reindexing
- Symbol-level branch diffs with git worktrees
- Noise filtering for common non-business calls

## Installation

```bash
pip install -e .
```

## CLI

```bash
codespine analyse /path/to/java-project --full
codespine search "payment processing" --k 20 --json
codespine impact com.example.Service#processPayment --max-depth 4 --json
codespine deadcode --limit 200 --json
codespine flow --max-depth 6 --json
codespine community --symbol com.example.Service --json
codespine coupling --months 6 --min-strength 0.3 --min-cochanges 3 --json
codespine watch --path . --global-interval 30
codespine diff main..feature --json
codespine stats
codespine start
codespine stop
```

## MCP Tools

- `search_hybrid(query, k=20)`
- `get_impact(symbol, max_depth=4)`
- `detect_dead_code(limit=200)`
- `trace_execution_flows(entry_symbol=None, max_depth=6)`
- `get_symbol_community(symbol)`
- `get_change_coupling(symbol=None, months=6, min_strength=0.3, min_cochanges=3)`
- `compare_branches(base_ref, head_ref)`
- `get_codebase_stats()`

## Runtime Files

- DB: `~/.codespine_db`
- PID: `~/.codespine.pid`
- Logs: `~/.codespine.log`

## Architecture

- `codespine/indexer`: Java parsing, symbol extraction, call resolution
- `codespine/db`: Kuzu schema and storage access
- `codespine/search`: BM25/fuzzy/vector/RRF hybrid ranking
- `codespine/analysis`: impact, dead code, flow, communities, coupling
- `codespine/diff`: worktree-based symbol diff
- `codespine/watch`: watch mode orchestration
- `codespine/mcp`: MCP tool server

## Compatibility

`gindex.py` remains as a compatibility shim for one release cycle.
