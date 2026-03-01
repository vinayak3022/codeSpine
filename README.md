# CodeSpine

CodeSpine is a Java-native code intelligence graph for coding agents.

It indexes your Java codebase into a graph, then serves high-signal retrieval and
analysis APIs over CLI + MCP for refactoring, impact analysis, architecture
navigation, and safe change planning.

## Why CodeSpine

Most tools answer "where is this symbol?".
CodeSpine answers:

- What depends on this?
- What else changed with this historically?
- Is this dead or framework-exempt?
- Which architectural cluster/flow is this in?
- What changed between branches at symbol granularity?

## Core Capabilities

### 1) Hybrid Search (BM25 + Vector + Fuzzy + RRF)
- Lexical ranking (BM25-based)
- Semantic matching (local embeddings)
- Typo-tolerant fuzzy matching
- Reciprocal Rank Fusion with ranking multipliers

### 2) Impact Analysis
- Traverses call graph + type/inheritance edges + coupling edges
- Groups results by depth (`1`, `2`, `3+`)
- Carries confidence (`1.0`, `0.8`, `0.5`) per edge

### 3) Java-Aware Dead Code Detection
- Not just zero-callers: includes exemption passes for:
- constructors, tests, `main(String[] args)`
- override/interface contracts
- common lifecycle/framework annotations
- reflection/bean-friendly method patterns

### 4) Execution Flow Tracing
- Detects framework-agnostic entry points (`main`, tests, public roots)
- BFS flow traces with depth
- Flow classification (`intra_community`, `cross_community`)

### 5) Community Detection
- Leiden-based clustering when dependencies are present
- Heuristic fallback when Leiden stack is unavailable
- Queryable symbol-to-community mapping

### 6) Git Change Coupling
- Mines recent git history (default 6 months)
- Links co-changing files with coupling strength
- Surfaces hidden dependencies in impact workflows

### 7) Watch Mode
- Live file watching for changed Java files
- Incremental reindexing
- Periodic global refresh phases (community/flow/deadcode/coupling)

### 8) Branch Diff (Symbol-Level)
- Uses git worktrees
- Diffs class/method symbols (`added`, `removed`, `modified`)
- Uses normalized structural hashes to reduce formatting-only noise

## Performance Model

CodeSpine includes:
- Hash-based incremental invalidation (only changed files reindexed)
- Persistent embedding cache (`sqlite`) for repeat semantic queries
- Transactional write path during indexing to reduce commit overhead

## Install

### Local editable install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

If your environment is externally managed (PEP 668), use a virtualenv as above.

You can also use `pip3`:

```bash
pip3 install -e .
```

### Install from GitHub

```bash
pip install "git+https://github.com/vinayak3022/codeSpine.git"
```

or

```bash
pip3 install "git+https://github.com/vinayak3022/codeSpine.git"
```

### Install from PyPI (after first release is published)

```bash
pip install codespine
```

or

```bash
pip3 install codespine
```

### Optional extras

- `pip install -e .[ml]` for local embedding model dependencies
- `pip install -e .[community]` for Leiden community detection stack
- `pip install -e .[full]` for all optional features

## Quick Start

```bash
# 1) index a repo
codespine analyse /path/to/java-project --full

# 2) search by concept/typo/name
codespine search "payment validation typo procss" --k 20 --json

# 3) get actionable context in one call
codespine context "processPayment" --max-depth 3 --json

# 4) estimate blast radius before refactor
codespine impact com.example.Service#processPayment(java.lang.String) --max-depth 4 --json
```

Example output:

```text
$ codespine analyse .
Walking files...               142 files found
Parsing code...                142/142
Tracing calls...               847 calls resolved
Analyzing types...             234 type relationships
Detecting communities...       8 clusters found
Detecting execution flows...   34 processes found
Finding dead code...           12 unreachable symbols
Analyzing git history...       18 coupled file pairs
Generating embeddings...       623 vectors stored

Done in 4.2s - 623 symbols, 1847 edges, 8 clusters, 34 flows
```

## CLI Commands

### Indexing and Retrieval
- `codespine analyse <path> [--full|--incremental]`
- `codespine search <query> [--k 20] [--json]`
- `codespine context <query> [--max-depth 3] [--json]`

### Analysis
- `codespine impact <symbol> [--max-depth 4] [--json]`
- `codespine deadcode [--limit 200] [--json]`
- `codespine flow [--entry <symbol>] [--max-depth 6] [--json]`
- `codespine community [--symbol <symbol>] [--json]`
- `codespine coupling [--months 6] [--min-strength 0.3] [--min-cochanges 3] [--json]`

### Operations
- `codespine watch [--path .] [--global-interval 30]`
- `codespine diff <base>..<head> [--json]`
- `codespine cypher <query> [--json]`
- `codespine list [--json]`
- `codespine stats`
- `codespine status [--json]`
- `codespine setup`
- `codespine clean [--force]`

### MCP Service
- `codespine start`
- `codespine stop`
- `codespine serve` (alias of `start`)
- `codespine mcp` (foreground stdio MCP)

## MCP JSON (Paste Into `mcp.json`)

Use this if your MCP client supports stdio servers:

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

If `codespine` is not on your PATH, use an absolute path for `command`, for example:
- macOS/Linux: `"/Users/<you>/path/to/venv/bin/codespine"`
- Windows: `"C:\\\\Users\\\\<you>\\\\path\\\\to\\\\venv\\\\Scripts\\\\codespine.exe"`

Optional working directory (recommended for repo-scoped usage):

```json
{
  "mcpServers": {
    "codespine": {
      "command": "codespine",
      "args": ["mcp"],
      "cwd": "/absolute/path/to/your/repo"
    }
  }
}
```

## MCP Tool Surface

- `search_hybrid(query, k=20)`
- `get_symbol_context(query, max_depth=3)`
- `get_impact(symbol, max_depth=4)`
- `detect_dead_code(limit=200)`
- `trace_execution_flows(entry_symbol=None, max_depth=6)`
- `get_symbol_community(symbol)`
- `get_change_coupling(symbol=None, months=6, min_strength=0.3, min_cochanges=3)`
- `compare_branches(base_ref, head_ref)`
- `get_codebase_stats()`
- `run_cypher(query)`

## Runtime Artifacts

- Graph DB: `~/.codespine_db`
- MCP PID: `~/.codespine.pid`
- Log file: `~/.codespine.log`
- Embedding cache: `~/.codespine_embedding_cache.sqlite3`

## Architecture

- `codespine/indexer`: Java parsing, symbols, call/type resolution
- `codespine/db`: Kuzu schema and persistence
- `codespine/search`: BM25/fuzzy/vector/RRF ranking
- `codespine/analysis`: impact/deadcode/flow/community/coupling/context
- `codespine/diff`: branch comparison at symbol level
- `codespine/watch`: incremental watch pipeline
- `codespine/mcp`: MCP tool server
- `codespine/noise`: noise blocklists for cleaner call graphs

## Security and Governance

- Security policy: [`SECURITY.md`](SECURITY.md)
- Contributions: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- Branch protection runbook: [`docs/GITHUB_HARDENING.md`](docs/GITHUB_HARDENING.md)

## Publish to PyPI

This repo includes a release workflow:
- [`.github/workflows/publish-pypi.yml`](.github/workflows/publish-pypi.yml)

Recommended setup (one-time):
1. Create project on PyPI with the same name (`codespine`) or update `project.name` if unavailable.
2. In PyPI, configure Trusted Publisher for this GitHub repo/workflow.
3. In GitHub, keep the `pypi` environment enabled for publishing.

Release flow:
1. Bump version in [`pyproject.toml`](pyproject.toml).
2. Push commit + tag (for example `v0.1.1`).
3. Create a GitHub Release for that tag.
4. Workflow builds and publishes to PyPI.

## Compatibility

`gindex.py` is retained as a compatibility shim for one release cycle.
