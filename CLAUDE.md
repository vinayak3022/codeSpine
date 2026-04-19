# CodeSpine (gindex) — Claude Code Guide

## Project
Python package `codespine` (also `gindex`). Local Java code intelligence indexer backed by KùzuDB graph database, exposed via a FastMCP server.

- **Entry point**: `codespine/cli.py` → `main` click group
- **MCP server**: `codespine/mcp/server.py` via `fastmcp`
- **Install**: `~/anaconda3/bin/pip install -e .`

## Architecture
| Component | File | Role |
|-----------|------|------|
| Graph store | `codespine/db/store.py` | KùzuDB with CQRS read replica |
| Schema | `codespine/db/schema.py` | DDL + migrations |
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
- **WAL**: Must call `CHECKPOINT` after DDL; read-only mode cannot replay WAL.
- **Overlay**: Incremental in-memory changes before full promote. `clear_overlay` wraps DB write in try/except since store may be read-only.
- **Project ID stability**: `engine.py` reuses existing project ID for same path to prevent drift.
- **Symbol normalization**: `_normalize_symbol_input()` in `server.py` strips `Class#` prefix from FQN inputs.

## DB / Schema
- Graph DB: KùzuDB (no SQLite)
- Embedding cache: JSON file at `~/.codespine_embedding_cache.json`
- Schema version: `'3'`
- `CO_CHANGED_WITH` rel uses `days INT64` (not `months`)

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
- Version in `codespine/__init__.py` and `pyproject.toml`
- PyPI publish triggers on `v*` tag push → `.github/workflows/publish-pypi.yml`
- **Always** push commit + tag: `git push origin main --tags`

## Tests
```
pytest tests/ -q
```
Tests use shared `~/.codespine_db` — always scope with `project=result.project_id`.

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
- KùzuDB C++ state poisoned after Ctrl+C mid-write → `unordered_map::at: key not found` on reopen. Use `force-reset` to recover.
- `rebuild_empty_db` must delete the read replica too (otherwise read-only callers see stale schema).
- `read_only=True` cannot replay WAL → always checkpoint after schema changes.
