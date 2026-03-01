# CodeSpine

CodeSpine is an intelligence layer for Java teams and AI coding agents.
It maps your codebase into a live graph so you can find anything fast, predict change impact, and ship safer refactors.

## Installation

```bash
pip install codespine
```

## Quick Start

```bash
codespine analyse .
codespine search "payment retry bug" --json
codespine context "processPayment" --json
codespine impact "com.example.Service#processPayment(java.lang.String)" --json
```

Example analyze output:

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

## What You Get

- Hybrid search: BM25 + semantic vectors + fuzzy + RRF
- Impact analysis: depth groups with confidence scoring
- Java-aware dead code detection with exemption passes
- Execution flow tracing from entry points
- Community detection (Leiden + fallback)
- Git change coupling analysis
- Watch mode incremental reindexing
- Symbol-level branch diff

## Key Commands

- `codespine analyse <path> [--full|--incremental]`
- `codespine search <query> [--k 20] [--json]`
- `codespine context <query> [--max-depth 3] [--json]`
- `codespine impact <symbol> [--max-depth 4] [--json]`
- `codespine deadcode [--limit 200] [--json]`
- `codespine flow [--entry <symbol>] [--max-depth 6] [--json]`
- `codespine community [--symbol <symbol>] [--json]`
- `codespine coupling [--months 6] [--min-strength 0.3] [--min-cochanges 3] [--json]`
- `codespine diff <base>..<head> [--json]`
- `codespine watch [--path .] [--global-interval 30]`

## MCP Setup (`mcp.json`)

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

## Runtime Paths

- `~/.codespine_db`
- `~/.codespine.pid`
- `~/.codespine.log`
- `~/.codespine_embedding_cache.sqlite3`

## Project Docs

- [`.github/CONTRIBUTING.md`](.github/CONTRIBUTING.md)
- [`.github/SECURITY.md`](.github/SECURITY.md)
- [`.github/CODE_OF_CONDUCT.md`](.github/CODE_OF_CONDUCT.md)
