# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project aims to follow
Semantic Versioning where practical.

## [Unreleased]

## [0.5.4] - 2026-03-14

### Changed

- Repositioned README around token savings for coding agents and clarified the graph-first MCP workflow.

## [0.3.0] - 2026-03-03

### Added

- **Workspace-level project detection** (`indexer/engine.py`, `cli.py`): `codespine analyse` now auto-detects whether the given path is a *workspace* folder (e.g. `~/IdeaProjects/`) containing multiple independent projects, or a single project. Independent projects are indexed under their own `Project` node; each project is still multi-module-aware. Stats, impact, dead-code, and flow analysis are all scoped per-project via the `project=` parameter. Cross-project querying remains available for integration-change analysis.
- **`start_watch` MCP tool** (`mcp/server.py`): agents can now start watch mode directly from MCP. The tool launches a background `codespine watch` subprocess, explains what watch does, and proactively recommends enabling it during development. Returns the PID and watched path.
- **`stop_watch` MCP tool** (`mcp/server.py`): gracefully terminates the background watch process.
- **`get_watch_status` MCP tool** (`mcp/server.py`): returns whether watch is running, the path, PID, and uptime. Shown in `get_capabilities` too.
- **`analyse_project` MCP tool** (`mcp/server.py`): agents can trigger project indexing without leaving the MCP session. Runs as a non-blocking background subprocess and returns immediately. Supports `full`, `deep`, and `embed` flags. Use `get_analyse_status()` to poll completion.
- **`get_analyse_status` MCP tool** (`mcp/server.py`): polls the background analysis job; returns running status, elapsed time, and the last 30 lines of output for progress/error context.
- **Background job state in `get_capabilities`** (`mcp/server.py`): `get_capabilities` now also reports `background_jobs` (watch running/path, analyse running/path) and surfaces a recommendation note when watch mode is not active.
- **`--embed/--no-embed` flag** (`cli.py`, `indexer/engine.py`): `codespine analyse` now defaults to `--no-embed` (skip vector embedding generation). This cuts indexing time from 10+ minutes to under a minute for large projects when `sentence-transformers` is installed, because `embed_text()` is no longer called per symbol. BM25 and fuzzy search continue to work; pass `--embed` to re-enable semantic vector search.
- **Parallel file parsing** (`indexer/engine.py`): Java files are now parsed concurrently using `ThreadPoolExecutor` (up to 8 workers). tree-sitter releases the GIL, giving real multi-core speedup. DB writes remain sequential inside the existing transaction.
- **`--allow-running` hidden flag** (`cli.py`): allows `codespine analyse` to run even when the MCP server is active (used by the `analyse_project` MCP tool). The MCP server opens the DB as `read_only=True` so a write-mode analysis subprocess can coexist.

### Changed

- `codespine analyse` now performs two levels of detection: workspace → projects → modules. A project is the key differentiator; modules within a project share the `{project}::{module}` ID scheme from 0.2.0.
- `IndexResult.embeddings_generated` is 0 when `embed=False`.

## [0.2.0] - 2026-03-03

### Fixed

- **SQLite thread-safety** (`search/vector.py`): embedding cache connections now opened with `check_same_thread=False`, eliminating the "SQLite objects created in a thread can only be used in that same thread" crash that blocked every non-trivial MCP tool call.
- **Kuzu buffer pool OOM** (`db/store.py`): reduced default buffer pool from 1 GB to 256 MB so the buffer manager can evict pages during large write batches; `set_community` now wraps its per-symbol MERGE loop in a single transaction, preventing exhaustion on 50 K+ symbol projects.
- **MCP concurrency** (`db/store.py`): replaced shared `self.conn` with thread-local Kuzu connections (`threading.local`). Each FastMCP worker thread (and each concurrent IDE agent) now gets its own connection, eliminating crashes when multiple agents access the index simultaneously. `kuzu.Database` is also opened with `read_only=True` in MCP mode (where supported by the Kuzu version) so multiple server processes can co-exist on the same DB file.

### Added

- **Multi-module project bifurcation** (`indexer/engine.py`, `cli.py`): `codespine analyse` now auto-detects Maven (`<modules>` in `pom.xml`) and Gradle (subdirectory build files) multi-module layouts and indexes each module as a separate `Project` node (`{root}::{module}`). Cross-module references are resolved across the shared graph. A module summary is printed before indexing begins.
- **`ping` MCP tool** (`mcp/server.py`): zero-cost connectivity check; returns `{"status": "ok", "version": "..."}`. Call this first to confirm the server is alive.
- **`get_capabilities` MCP tool** (`mcp/server.py`): returns indexed projects, symbol counts, and a feature-flag map showing exactly which tools are ready to use, which require `--deep`, and which require optional dependencies. Eliminates trial-and-error by agents.
- **`list_projects` MCP tool** (`mcp/server.py`): lists all indexed projects with symbol and file counts.
- **`project` parameter** on `search_hybrid`, `get_impact`, `get_symbol_context`, `detect_dead_code`, `trace_execution_flows` (`mcp/server.py` + analysis modules): pass `project=<project_id>` to scope results to a single module; omit to query all projects (cross-project references intact).
- **`git_log` MCP tool** (`mcp/server.py`): recent git commits for the project or a specific file. Returns `available: false` gracefully when not in a git repo.
- **`git_diff` MCP tool** (`mcp/server.py`): working-tree diff (or between two refs), truncated at 200 lines with a `truncated` flag.
- **`available` flag on all MCP tool responses**: every tool now returns `available: false` with an explanatory `note` when data is missing (not indexed, wrong project, missing dependency), so agents know immediately what to do without further probing.
- **Module-aware watch mode** (`watch/watcher.py`): `codespine watch <path>` now detects the module structure at startup and re-indexes only the affected module when files change, rather than the entire tree.

## [0.1.9] - 2026-03-03

### Changed

- Switched `codespine analyse` default mode to incremental (`--incremental`).
- Added persistent file metadata cache (`mtime`, `size`, `hash`) to avoid re-hashing unchanged files on incremental runs.
- Added early no-op short-circuit when no Java files changed, reducing repeat analyze latency.
- Added regression coverage for zero-reindex incremental runs.

## [0.1.8] - 2026-03-03

### Changed

- Fixed multi-module indexing collisions by introducing module-scoped class/method/symbol IDs.
- Improved resolver fidelity for multi-module projects by preferring exact class-ID matches for intra-class calls.
- Added regression coverage for duplicate FQCNs across modules.

## [0.1.7] - 2026-03-02

### Changed

- Added automatic fast-path for large repositories in `codespine analyse`:
  - global heavy analyses are skipped by default on very large repos
  - rerun with `--deep` to force full post-index analysis
- Capped low-confidence fuzzy call expansion to avoid edge explosion and improve large-project indexing latency.

## [0.1.6] - 2026-03-02

### Changed

- Added visible progress updates for long post-parse phases in `codespine analyse` (`Tracing calls`, `Analyzing types`).
- Streamed call resolution edges instead of building a full in-memory list first, improving large-project latency and memory pressure.

## [0.1.5] - 2026-03-02

### Changed

- Improved `codespine analyse` UX with live parse progress updates during indexing.
- Reduced full-analysis overhead by removing redundant file hash pre-pass.
- Faster file discovery by pruning common heavy build/IDE directories.
- Synced package version metadata across `pyproject.toml` and `codespine/__init__.py`.

## [0.1.4] - 2026-03-02

### Added

- Full `codespine/` package refactor with modular architecture.
- Hybrid search: BM25 + semantic vectors + fuzzy + RRF.
- Impact analysis command/tool with depth-grouped outputs and confidence edges.
- Java-aware dead code analysis command/tool.
- Execution flow tracing command/tool.
- Community detection command/tool (Leiden + fallback).
- Git change coupling analysis command/tool.
- Watch mode command with incremental indexing and periodic global refresh.
- Branch comparison command/tool with symbol-level add/modify/remove output.
- Noise filtering blocklist for call graph cleanup.
- New CLI surfaces (`impact`, `deadcode`, `flow`, `community`, `coupling`, `watch`, `diff`).
- Additional advanced CLI surfaces (`context`, `cypher`, `list`, `status`, `clean`, `setup`, `serve`, `mcp`).
- MCP server expanded with advanced tools.
- Compatibility shim retained in `gindex.py`.
- Fourth-pass performance tuning:
  - hash-based incremental index invalidation
  - persistent SQLite embedding cache
  - transactional batched write path for indexing
- Packaging/installability improvements:
  - relaxed build backend version pin
  - optional dependency extras (`ml`, `community`, `full`)
  - README install instructions for both `pip` and `pip3`
- Added automated PyPI publish workflow via GitHub Releases (`publish-pypi.yml`).

## [0.1.0] - 2026-03-02

### Added

- Initial CodeSpine CLI for Java graph indexing and MCP integration.
