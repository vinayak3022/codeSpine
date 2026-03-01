"""Compatibility shim for one release cycle.

Use `codespine` CLI entrypoint moving forward.
"""

from codespine.cli import main


if __name__ == "__main__":
    main()
