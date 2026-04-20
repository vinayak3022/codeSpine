# CodeSpine (gindex) — Claude Code Guide

## Project
Python package `codespine` (also `gindex`). Local Java code intelligence indexer backed by **DuckDB** (primary, since v1.0.0) or KùzuDB (alternate), exposed via a FastMCP server.

- **Entry point**: `codespine/cli.py` → `main` click group
- **MCP server**: `codespine/mcp/server.py` via `fastmcp`
- **Install**: `~/anaconda3/bin/pip install -e .` (or `pip install codespine`)
- **Backend selector**: `CODESPINE_BACKEND=duckdb|kuzu` env var (default: `duckdb`)

## Architecture
| Component | File | Role |
|-----------|------|------|
| DuckDB store | `codespine/db/duckdb_store.py` | Primary SQL backend, Cypher-transparent |
| Cypher→SQL | `codespine/db/_cypher_compat.py` | Translates all 91 Cypher call-sites to DuckDB SQL |
| Kuzu store | `codespine/db/store.py` | Alternate KùzuDB backend |
| Sharded store | `codespine/sharding/store.py` | `ShardedGraphStore`: consistent-hash, 4 shards |
| Schema | `codespine/db/schema.py` | DDL + migrations |
| Config | `codespine/config.py` | `CodeSpineConfig` dataclass; backend default |
| Indexer | `codespine/indexer/engine.py` | Parse + upsert Java files |
| Parser | `codespine/indexer/java_parser.py` | tree-sitter Java |
| Call resolver | `codespine/indexer/call_resolver.py` | Method call edge resolution |
| Cross-module | `codespine/analysis/crossmodule.py` | Cross-module call linking |
| Search | `codespine/search/hybrid.py` | BM25 + vector + fuzzy via RRF |
| MCP tools | `codespine/mcp/server.py` | 32 FastMCP tools |
| Guide | `codespine/guide.py` | Single source of truth for guide content |
| Watch | `codespine/watch/watcher.py` | File watcher + overlay system |

## Key Patterns
- **CQRS**: Write DB at `~/.codespine_db`, read replica at `~/.codespine_db_read`. Always call `snapshot_to_read_replica()` after mutations.
- **Sub-batched writes**: 200 methods/symbols per transaction with `_recycle_conn()` between each to prevent OOM.
- **WAL**: Must call `CHECKPOINT` before any file-copy snapshot (DuckDB WAL must be flushed first).
- **Overlay**: Incremental in-memory changes before full promote. `clear_overlay` wraps DB write in try/except since store may be read-only.
- **Project ID stability**: `engine.py` reuses existing project ID for same path to prevent drift.
- **Symbol normalization**: `_normalize_symbol_input()` in `server.py` strips `Class#` prefix from FQN inputs.
- **CLI store access**: All CLI commands use `_open_store(read_only=bool)` → returns `ShardedGraphStore`. Never instantiate `GraphStore` / `DuckDBGraphStore` directly in CLI.

## DB / Schema
- **Primary backend**: DuckDB (single `.db` file per shard, ACID, SQL)
- **Alternate backend**: KùzuDB (directory per shard, Cypher)
- **Backend env var**: `CODESPINE_BACKEND=duckdb` (default) or `CODESPINE_BACKEND=kuzu`
- Embedding cache: JSON file at `~/.codespine_embedding_cache.json`
- Schema version: `'3'`
- `CO_CHANGED_WITH` rel uses `days INT64` (not `months`)

## DuckDB Backend — Critical Details
- `duckdb_store.py` **pre-flight probe**: opens a temp connection to detect legacy KùzuDB artifact (a directory where a file is expected) at `db_path` or `snapshot_path`. If a directory is found, it is deleted before the real `connect()`.
- `duckdb_store.py` **read-only + missing file**: opens `:memory:` instead of crashing (MCP server startup before first index).
- `query_records()` auto-detects Cypher via `is_cypher()`, translates via `translate()`, passes params as `dict` (named `$param` style). Plain SQL is passed through unchanged.
- `_do_snapshot()` calls `CHECKPOINT` before `shutil.copy2()` to flush WAL to file.

## Cypher→SQL Translator (`_cypher_compat.py`)
- `is_cypher(query)` → True if query starts with `MATCH`
- `translate(cypher, params)` → `(sql, params_dict)`
- `_LABEL_TABLE`: maps Kuzu node labels (`Project`, `File`, `Class`, `Method`, `Symbol`, `Community`, `Flow`) to DuckDB table names
- `_REL_EDGE`: maps 8 relationship types (`CALLS`, `OVERRIDES`, `IMPLEMENTS`, `INJECTS`, `BINDS_INTERFACE`, `IN_COMMUNITY`, `IN_FLOW`, `CO_CHANGED_WITH`) to `(edge_table, src_col, dst_col, extra_where)`
- All 7 edge tables: `calls`, `references_type`, `injects`, `binds_interface`, `community_members`, `flow_members`, `co_changed_with`
- **Anonymous edge pattern** `MATCH ()-[r]->() RETURN count(r)`: special-cased to `UNION ALL` of row counts across all 7 edge tables — never falls through to `dual`
- **Fallback FROM**: `(SELECT 1 WHERE 1=0) _empty(x)` — DuckDB-valid empty relation, not Oracle's `dual`

## ShardedGraphStore (`sharding/store.py`)
- Consistent-hash sharding across 4 shards by project root (prefix before `::`)
- `list_project_metadata()` fans out across all shards
- `query_records(cypher, params)` fans out to all shards, deduplicates results
- `snapshot_all(background=False)` snapshots all shards; use after index mutations
- `clear_analysis_artifacts()`, `rebuild_empty_db()`, `snapshot_to_read_replica(background)` are fan-out wrappers

## Multi-Module Projects
- Multi-module project IDs: `{root_basename}::{module_basename}`
- `analyse` auto-detects Maven/Gradle modules and iterates them
- Module co-location: root and all `root::*` modules always land on the same shard

## MCP Tools (32 total)
All tools return pre-serialized JSON string via `_json()` — never return raw dicts.
All analysis tools accept optional `project=` for scoping.
Agent workflow: call `guide()` first, then `get_capabilities()`.

## CLI Commands
```
analyse, search, context, impact, deadcode, flow, community, coupling,
watch, diff, stats, list, status, guide, setup,
overlay-status/promote/clear, clear-project, clear-index, force-reset,
start, stop, mcp
```

## Publishing
- Version in `codespine/__init__.py` **and** `pyproject.toml` (must match)
- PyPI publish triggers on `v*` tag push → `.github/workflows/publish-pypi.yml`
- **Always** push commit + tag: `git push origin main --tags`
- Install to test locally: `~/anaconda3/bin/pip install -e .`

## Tests
```
pytest tests/ -q
```
- Tests use shared `~/.codespine_db` — always scope with `project=result.project_id`
- `tests/test_cypher_compat.py` — 38 Cypher→SQL translator tests; add regressions here for new patterns
- `tests/test_duckdb_store.py` — store-level tests including legacy-artifact cleanup, snapshot, ShardedGraphStore

## Running locally
```bash
# Install
~/anaconda3/bin/pip install -e .

# Index a Java project
codespine analyse /path/to/java-project

# Start MCP server (stdio)
codespine mcp

# Start MCP background daemon
codespine start
```

## Known Gotchas

### DuckDB
- **`FROM dual` crash**: DuckDB has no `dual` table (Oracle convention). Any untranslated Cypher that falls through the translator will now produce an empty result (via `_empty` fallback), not a crash. Anonymous edge patterns (`MATCH ()-[r]->()`) are special-cased correctly.
- **File vs directory**: DuckDB stores each shard as a *single file*. Legacy KùzuDB stored each shard as a *directory*. If a user upgrades from KùzuDB, the old directories are silently cleaned up by the pre-flight probe on open.
- **Snapshot requires CHECKPOINT**: DuckDB's WAL is separate from the DB file. Always call `CHECKPOINT` before `shutil.copy2()` or the snapshot will be missing recent writes.
- **Named params only**: DuckDB `execute(sql, dict)` works with `$name` placeholders. For `?` placeholders you must pass a list. The translator always produces `$name` style so Cypher-translated queries always use dict.

### KùzuDB (alternate backend)
- C++ state poisoned after Ctrl+C mid-write → `unordered_map::at: key not found` on reopen. Use `force-reset` to recover.
- `rebuild_empty_db` must delete the read replica too (otherwise read-only callers see stale schema).
- `read_only=True` cannot replay WAL → always checkpoint after schema changes.

### General
- `analyse` summary stats (`symbol_count`, `edge_count`, `vector_count`) are wrapped in `_safe_count()` — they print a yellow warning and return 0 on any failure rather than crashing after a successful 60-second index.
