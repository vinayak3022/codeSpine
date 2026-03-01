# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project aims to follow
Semantic Versioning where practical.

## [Unreleased]

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
- MCP server expanded with advanced tools.
- Compatibility shim retained in `gindex.py`.

## [0.1.0] - 2026-03-02

### Added

- Initial CodeSpine CLI for Java graph indexing and MCP integration.
