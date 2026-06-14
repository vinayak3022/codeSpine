## Goal

Turn the full Graph-Based Code Intelligence vision for CodeSpine/gindex into an aggressive, implementation-ready execution backlog with ordered slices, explicit dependencies, acceptance criteria, and a recommended first coding tranche.

## Phase

Backlog synthesis

## Status

complete

## Tasks

- [x] Review repository structure and current architecture
- [x] Identify major product capabilities and implementation surfaces
- [x] Inventory current delivery surface across indexer, store, search, CLI, MCP, watch, and tests
- [x] Convert target product vision into ordered implementation slices
- [x] Define dependencies and acceptance criteria per slice
- [x] Recommend the best first coding tranche based on leverage and risk

## Decisions

- Use the current repository state as the baseline and express the backlog as deltas from what already exists.
- Treat DuckDB + sharded storage + read-replica publishing as fixed architectural constraints.
- Prioritize slices that improve correctness and observability before adding more LLM-facing surface area.
- Frame the backlog around vertical slices that each end with user-visible capability gains and testable acceptance criteria.

## Blockers

- The exact “full Graph-Based Code Intelligence prompt” text is not present in the repo context, so the backlog is inferred from the current README, CLAUDE guide, code surface, and tests.
