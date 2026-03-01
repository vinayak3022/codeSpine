# Contributing to CodeSpine

Thanks for contributing.

## Development Setup

1. Clone the repo.
2. Create and activate a virtual environment.
3. Install in editable mode:

```bash
pip install -e .
```

## Local Checks

Run these before opening a PR:

```bash
python -m compileall codespine gindex.py
python -m codespine.cli --help
```

## Pull Request Guidelines

- Keep PRs focused and small where possible.
- Include a clear description of what changed and why.
- Update `README.md` and `CHANGELOG.md` for user-visible changes.
- Add tests when behavior changes.

## Commit Message Guidance

Prefer clear, imperative messages, for example:

- `fix: avoid stale pid false-positive in stop command`
- `docs: add MCP usage section`
- `ci: add python 3.10-3.13 test matrix`

## Reporting Bugs

Use the bug report template and include:

- OS and Python version
- Exact command run
- Full traceback or log excerpts
- Minimal reproduction steps

## Code Style

- Follow PEP 8.
- Keep functions small and explicit.
- Avoid broad `except:` blocks when possible.
