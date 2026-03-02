# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project aims to follow
Semantic Versioning where practical.

## [Unreleased]

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
