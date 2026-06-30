"""
Shared helpers, telemetry, proxy classes, and tool context for the CodeSpine
MCP server.

Extracted from ``server.py`` to break up the 3331-line monolith.
"""

from __future__ import annotations

import dataclasses
import inspect
import json as _json_mod
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from typing import Any, Callable

__all__ = [
    "_json",
    "_safe_tool_response",
    "_git_available",
    "_resolve_repo_path",
    "_no_symbols_response",
    "_index_guard",
    "_normalize_symbol_input",
    "_preferred_symbol_inputs",
    "_parse_project_symbol",
    "_cross_project_guidance",
    "_parse_indexed_at",
    "MCPTelemetry",
    "_staleness_meta",
    "_project_inventory",
    "_sum_count_rows",
    "_store_snapshot_mtime",
    "_store_snapshot_mtime_ns",
    "_overlay_snapshot_mtime",
    "_overlay_snapshot_mtime_ns",
    "_reload_store_instance",
    "_StoreProxy",
    "_WATCH_ACTIVE",
    "_set_watch_active",
]

_LOGGER = logging.getLogger(__name__)

_serial = threading.Lock()
_WATCH_ACTIVE: bool = False


def _set_watch_active(value: bool) -> None:
    """Set the module-level watch-active flag (used by tools in build_mcp_server)."""
    global _WATCH_ACTIVE
    _WATCH_ACTIVE = value


def _get_watch_active() -> bool:
    """Return the current watch-active flag state."""
    return _WATCH_ACTIVE


# ── Serialisation / error helpers ──────────────────────────────────────────────


def _json(data: dict, *, preserve_empty_keys: set[str] | frozenset[str] = frozenset()) -> str:
    """Serialize response dict to a JSON string.

    FastMCP double-serialises dict return values on many transports (SSE,
    stdio) producing duplicate JSON payloads that waste ~50 K tokens/session.
    Returning a pre-serialised string guarantees a single TextContent block.

    Strips None values and empty containers to keep payloads compact.
    """
    cleaned = {
        k: v
        for k, v in data.items()
        if v is not None and (k in preserve_empty_keys or (v != [] and v != {}))
    }
    return _json_mod.dumps(cleaned, separators=(",", ":"))


def _safe_tool_response(fn_name: str, exc: Exception) -> str:
    """Return a structured JSON error string — never a raw stack trace."""
    msg = str(exc)
    # Detect OOM / buffer pool exhaustion.
    is_oom = isinstance(exc, MemoryError) or any(
        m in msg.lower() for m in ("buffer pool", "out of memory", "oom", "cannot allocate")
    )
    return _json({
        "available": False,
        "error": "Analysis truncated due to memory limits." if is_oom else f"Tool error: {msg[:300]}",
        "truncated": is_oom,
        "tool": fn_name,
    })


# ── Git / project helpers ──────────────────────────────────────────────────────


def _git_available(path: str) -> bool:
    """Return True if path is inside a git repository."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception as exc:
        _LOGGER.debug("_git_available failed (fallback False): %s", exc)
        return False


def _resolve_repo_path(store, project: str | None, repo_path_provider) -> str:
    """Resolve the filesystem path for a given project_id, falling back to cwd."""
    if project:
        try:
            recs = store.query_records(
                "MATCH (p:Project) WHERE p.id = $pid RETURN p.path as path LIMIT 1",
                {"pid": project},
            )
            if recs and recs[0].get("path"):
                return recs[0]["path"]
        except Exception as exc:
            _LOGGER.debug("_resolve_repo_path query failed, fallback to provider: %s", exc)
    return repo_path_provider()


def _no_symbols_response(
    note: str = "No symbols indexed. Run 'codespine analyse <path>' first.",
) -> str:
    return _json({"available": False, "note": note})


def _index_guard(store) -> str | None:
    """Return a JSON failure when the index appears empty or corrupted."""
    try:
        project_rows = store.query_records("MATCH (p:Project) RETURN count(p) as n")
        symbol_rows = store.query_records("MATCH (s:Symbol) RETURN count(s) as n")
        projects = _sum_count_rows(project_rows)
        symbols = _sum_count_rows(symbol_rows)
        if projects > 0 and symbols == 0:
            return _no_symbols_response(
                "Index appears empty or corrupted (projects exist but 0 symbols are readable). "
                "Run 'codespine health' and 'codespine repair --full <path>' or restart after stopping stale watch processes."
            )
    except Exception as exc:
        return _no_symbols_response(f"Index is unavailable: {str(exc)[:200]}")
    return None


# ── Symbol normalisation ────────────────────────────────────────────────────────


def _normalize_symbol_input(raw: str) -> str:
    """Normalize a symbol string so that various user input formats work.

    Handles:
      - ``com.example.MyClass#myMethod(int,String)`` → ``myMethod(int,String)``
      - ``MyClass#myMethod``                         → ``myMethod``
      - ``myMethod(int,String)``                     → unchanged
      - ``myMethod``                                 → unchanged
    """
    s = raw.strip()
    if "#" in s:
        s = s[s.index("#") + 1:]
    return s


def _preferred_symbol_inputs(raw: str) -> list[str]:
    original = raw.strip()
    normalized = _normalize_symbol_input(raw)
    candidates: list[str] = []
    if original:
        candidates.append(original)
    if normalized and normalized != original:
        candidates.append(normalized)
    return candidates


def _parse_project_symbol(symbol: str, project: str | None = None) -> tuple[str | None, str]:
    """Parse ``project::SymbolName`` shorthand syntax.

    If *symbol* contains ``::``, extract the project prefix and the actual
    symbol name.  An explicit *project* keyword argument always takes
    precedence over the inline prefix.

    Returns ``(project, symbol_name)`` — the resolved project (possibly None)
    and the cleaned symbol string.
    """
    s = symbol.strip()
    if "::" in s:
        parts = s.split("::", 1)
        prefix = parts[0].strip()
        rest = parts[1].strip()
        if prefix and rest:
            # Inline prefix only applies when no explicit project was given.
            if project is None:
                project = prefix
            return project, rest
    return project, s


def _cross_project_guidance(store) -> str:
    """Check for cross-project reference edges and return actionable guidance.

    Returns an empty string when cross-project data already exists or when
    fewer than 2 projects are present.
    """
    try:
        proj_recs = store.query_records(
            "MATCH (p:Project) RETURN count(p) as n"
        )
        project_count = _sum_count_rows(proj_recs)
        if project_count < 2:
            return ""
        # Check for any REFERENCES_TYPE edges where src and dst are in
        # different projects.
        ref_rows = store.query_records(
            """
            MATCH (src:Symbol)-[r:REFERENCES_TYPE]->(dst:Symbol), (sf:File), (df:File)
            WHERE src.file_id = sf.id AND dst.file_id = df.id AND sf.project_id <> df.project_id
            RETURN count(r) as n
            LIMIT 1
            """
        )
        ref_count = _sum_count_rows(ref_rows)
        if ref_count > 0:
            return ""  # cross-project data exists, no guidance needed
        return (
            "Hint: 2+ projects are indexed but no cross-project import references were found. "
            "Run 'codespine analyse --complete <workspace>' with --complete to enable "
            "import-resolution linking across projects."
        )
    except Exception:
        return ""


def _parse_indexed_at(raw) -> int:
    """Robustly parse an indexed_at value that may be str, int, float, or None."""
    if raw is None:
        return 0
    try:
        val = int(float(str(raw)))
        # Sanity check: must look like a Unix timestamp (> year 2000)
        return val if val > 946684800 else 0
    except (ValueError, TypeError):
        return 0


# ── Telemetry ───────────────────────────────────────────────────────────────────


class MCPTelemetry:
    """Thread-safe call-level telemetry for MCP tools.

    Tracks per-tool call count, error count, and average latency.
    Exposed via ``get_telemetry()`` and ``get_capabilities()``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tool_calls: dict[str, int] = defaultdict(int)
        self._tool_errors: dict[str, int] = defaultdict(int)
        self._tool_latencies: dict[str, list[float]] = defaultdict(list)
        self._started_at = time.time()

    def record_call(self, tool_name: str, duration: float, *, error: bool = False) -> None:
        with self._lock:
            self._tool_calls[tool_name] += 1
            self._tool_latencies[tool_name].append(duration)
            if error:
                self._tool_errors[tool_name] += 1

    def snapshot(self) -> dict:
        with self._lock:
            tools: dict[str, dict] = {}
            for name in sorted(self._tool_calls):
                latencies = self._tool_latencies.get(name, [])
                avg_ms = round((sum(latencies) / len(latencies)) * 1000, 1) if latencies else None
                tools[name] = {
                    "calls": self._tool_calls[name],
                    "errors": self._tool_errors.get(name, 0),
                    "avg_latency_ms": avg_ms,
                }
            return {
                "uptime_s": round(time.time() - self._started_at),
                "total_calls": sum(self._tool_calls.values()),
                "total_errors": sum(self._tool_errors.values()),
                "tools": tools,
            }

    @property
    def started_at(self) -> float:
        return self._started_at


# ── Staleness / metadata ───────────────────────────────────────────────────────


def _staleness_meta(
    store,
    response: dict,
    project: str | None = None,
    overlay_store=None,
    deep_scope: bool = False,
    compact: bool = True,
    preserve_empty_keys: set[str] | frozenset[str] = frozenset(),
    watch_is_active: bool | None = None,
) -> str:
    """Inject index staleness metadata into every tool response and serialise.

    In compact mode (the default) only fields that carry actionable information
    are included — stale_warning when the index is > 1 h old, and overlay
    status only when files are actually dirty.  This saves ~200-400 tokens per
    call compared to always emitting all metadata fields.

    Stale warnings are placed FIRST in the response dict so they are seen
    immediately by LLM consumers.

    Returns a JSON string (not a dict) to avoid FastMCP double-serialisation.
    """
    prefix: dict = {}
    try:
        if project:
            recs = store.query_records(
                "MATCH (p:Project) WHERE p.id = $pid RETURN p.indexed_at as ts",
                {"pid": project},
            )
        else:
            recs = store.query_records(
                "MATCH (p:Project) RETURN p.indexed_at as ts ORDER BY p.indexed_at ASC LIMIT 1"
            )
            if recs:
                ts = _parse_indexed_at(recs[0].get("ts"))
                if ts:
                    age = int(time.time()) - ts
                    if age > 3600:
                        if watch_is_active if watch_is_active is not None else _WATCH_ACTIVE:
                            # Watch is active; downgrade warning to a note.
                            prefix["stale_warning"] = (
                                f"A snapshot from {age // 3600}h {(age % 3600) // 60}m ago is being served; "
                                "watch daemon is active and results will refresh within ~30s."
                            )
                        else:
                            prefix["stale_warning"] = (
                                f"Index is {age // 3600}h {(age % 3600) // 60}m old. "
                                "Run analyse_project() or start_watch() to refresh."
                            )
                if not compact:
                    response["index_age_seconds"] = age
                    response["indexed_at_epoch"] = ts
    except Exception as exc:
        _LOGGER.debug("Staleness check skipped: %s", exc)

    if overlay_store is not None:
        try:
            from codespine.overlay.merge import overlay_summary

            summary = overlay_summary(overlay_store, project=project)
            if compact:
                if summary.get("overlay_present"):
                    response["overlay_dirty_projects"] = summary.get("dirty_projects", [])
                    response["overlay_dirty_files"] = summary.get("dirty_file_count", 0)
                    if summary.get("deleted_file_count"):
                        response["overlay_deleted_files"] = summary["deleted_file_count"]
            else:
                response.update(summary)
            if project and not compact:
                meta = store.get_project_metadata(project) or {}
                response["base_indexed_commit"] = meta.get("indexed_commit", "")
                from codespine.overlay.git_state import current_head

                response["working_head_commit"] = current_head(
                    meta.get("path") or response.get("path") or ""
                )
            if deep_scope and summary.get("overlay_present"):
                response["overlay_excluded"] = True
                response["note"] = "Results reflect committed index only; uncommitted overlay changes are excluded."
        except Exception as exc:
            _LOGGER.debug("Overlay staleness check skipped: %s", exc)

    final = {**prefix, **response}
    return _json(final, preserve_empty_keys=preserve_empty_keys)


# ── Project inventory ────────────────────────────────────────────────────────────


def _project_inventory(store) -> list[dict]:
    from codespine.project_state import (
        derive_project_status,
        list_project_states,
        snapshot_info,
        synthetic_project_state,
    )

    state_by_id = {
        item.get("project_id"): item
        for item in list_project_states()
        if item.get("project_id")
    }
    try:
        projects = (
            store.list_project_metadata()
            if hasattr(store, "list_project_metadata")
            else store.query_records(
                "MATCH (p:Project) RETURN p.id as id, p.path as path, p.indexed_at as indexed_at"
            )
        )
    except Exception as exc:
        _LOGGER.debug("Project inventory query failed (fallback []): %s", exc)
        projects = []
    meta_by_id = {item.get("id"): item for item in projects if item.get("id")}
    project_ids = sorted({pid for pid in list(meta_by_id) + list(state_by_id) if pid})
    out: list[dict] = []
    for pid in project_ids:
        project = meta_by_id.get(pid, {})
        state = state_by_id.get(pid) or synthetic_project_state(
            pid, path=project.get("path", "")
        )
        snap = snapshot_info(pid, store.router if hasattr(store, "router") else None)
        state_only = not bool(project)
        out.append(
            {
                "project_id": pid,
                "path": state.get("path") or project.get("path"),
                "indexed_at": project.get("indexed_at"),
                "project_state": derive_project_status(state, snap),
                "core_state": state.get("core_state"),
                "deep_state": state.get("deep_state"),
                "last_error": state.get("last_error"),
                "repair_hint": state.get("repair_hint"),
                "snapshot_valid": snap.get("snapshot_valid"),
                "write_db_valid": snap.get("write_db_valid"),
                "state_only": state_only,
                "inventory_source": "state" if state_only else "db",
            }
        )
    return out


# ── Counting ─────────────────────────────────────────────────────────────────────


def _sum_count_rows(rows: list[dict]) -> int:
    total = 0
    for row in rows or []:
        if "count" in row:
            total += int(row["count"] or 0)
        elif "n" in row:
            total += int(row["n"] or 0)
        elif "total" in row:
            total += int(row["total"] or 0)
        elif "linked" in row:
            total += int(row["linked"] or 0)
        else:
            total += int(next(iter(row.values()), 0) or 0)
    return total


# ── Snapshot / sentinel mtime helpers ───────────────────────────────────────────


def _snapshot_mtime_for_path(path: str) -> float:
    try:
        if path and os.path.exists(path):
            return os.path.getmtime(path)
    except OSError:
        pass
    return 0.0


def _snapshot_mtime_ns_for_path(path: str) -> int:
    try:
        if path and os.path.exists(path):
            return os.stat(path).st_mtime_ns
    except OSError:
        pass
    return 0


def _store_snapshot_mtime(store, project: str | None = None) -> float:
    try:
        router = getattr(store, "router", None)
        if router is not None and hasattr(router, "all_shards") and hasattr(router, "snapshot_path"):
            shard_ids = list(router.all_shards())
            mtimes = [
                _snapshot_mtime_for_path(router.snapshot_path(idx) + ".updated")
                for idx in shard_ids
            ]
            return max(mtimes, default=0.0)
        snapshot_path = getattr(store, "_snapshot_path", "")
        return _snapshot_mtime_for_path(snapshot_path + ".updated")
    except Exception as exc:
        _LOGGER.debug("store_snapshot_mtime fallback 0: %s", exc)
        return 0.0


def _store_snapshot_mtime_ns(store, project: str | None = None) -> int:
    try:
        router = getattr(store, "router", None)
        if router is not None and hasattr(router, "all_shards") and hasattr(router, "snapshot_path"):
            shard_ids = list(router.all_shards())
            mtimes = [
                _snapshot_mtime_ns_for_path(router.snapshot_path(idx) + ".updated")
                for idx in shard_ids
            ]
            return max(mtimes, default=0)
        snapshot_path = getattr(store, "_snapshot_path", "")
        return _snapshot_mtime_ns_for_path(snapshot_path + ".updated")
    except Exception as exc:
        _LOGGER.debug("store_snapshot_mtime_ns fallback 0: %s", exc)
        return 0


def _overlay_snapshot_mtime(store, project: str | None = None) -> float:
    try:
        overlay_store = getattr(store, "overlay_store", None)
        if overlay_store is None:
            return 0.0
        if project:
            return _snapshot_mtime_for_path(overlay_store.project_path(project))
        mtimes = []
        for doc in overlay_store.list_projects():
            project_id = doc.get("project_id")
            if project_id:
                mtimes.append(
                    _snapshot_mtime_for_path(overlay_store.project_path(project_id))
                )
        return max(mtimes, default=0.0)
    except Exception as exc:
        _LOGGER.debug("overlay_snapshot_mtime fallback 0: %s", exc)
        return 0.0


def _overlay_snapshot_mtime_ns(store, project: str | None = None) -> int:
    try:
        overlay_store = getattr(store, "overlay_store", None)
        if overlay_store is None:
            return 0
        if project:
            return _snapshot_mtime_ns_for_path(overlay_store.project_path(project))
        mtimes = []
        for doc in overlay_store.list_projects():
            project_id = doc.get("project_id")
            if project_id:
                mtimes.append(
                    _snapshot_mtime_ns_for_path(overlay_store.project_path(project_id))
                )
        return max(mtimes, default=0)
    except Exception as exc:
        _LOGGER.debug("overlay_snapshot_mtime_ns fallback 0: %s", exc)
        return 0


# ── Store reload / proxy ────────────────────────────────────────────────────────


def _reload_store_instance(store):
    cls = type(store)
    params = inspect.signature(cls).parameters
    kwargs = {}
    if "read_only" in params:
        kwargs["read_only"] = True
    if "backend" in params and hasattr(store, "backend"):
        kwargs["backend"] = getattr(store, "backend")
    if "db_path_override" in params and hasattr(store, "_db_path"):
        kwargs["db_path_override"] = getattr(store, "_db_path")
    if "snapshot_path_override" in params and hasattr(store, "_snapshot_path"):
        kwargs["snapshot_path_override"] = getattr(store, "_snapshot_path")
    if "num_shards" in params and hasattr(store, "router"):
        kwargs["num_shards"] = getattr(store.router, "num_shards", None)
    if "shards_dir" in params and hasattr(store, "router"):
        kwargs["shards_dir"] = getattr(store.router, "shards_dir", None)
    return cls(**kwargs)


class _StoreProxy:
    """Wraps a GraphStore and hot-reloads from the read replica when the
    post-analyse sentinel file is touched.

    After ``codespine analyse`` finishes it copies the write DB to
    ``~/.codespine_db_read`` and writes ``~/.codespine_db_read.updated``.
    This proxy checks that sentinel's mtime before every attribute access and
    silently swaps in a fresh read-only GraphStore so the MCP daemon picks up
    the new index without restarting.
    """

    def __init__(self, store) -> None:
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_last_mtime", self._sentinel_mtime())

    def _sentinel_mtime(self) -> float:
        return _store_snapshot_mtime(object.__getattribute__(self, "_store"))

    def _maybe_reload(self) -> None:
        current = self._sentinel_mtime()
        if current > object.__getattribute__(self, "_last_mtime"):
            try:
                new_store = _reload_store_instance(
                    object.__getattribute__(self, "_store")
                )
                object.__setattr__(self, "_store", new_store)
                object.__setattr__(self, "_last_mtime", current)
                _LOGGER.info(
                    "MCP: hot-reloaded %s from updated snapshot",
                    type(new_store).__name__,
                )
            except Exception as exc:
                _LOGGER.warning("MCP: hot-reload failed: %s", exc)

    def __getattr__(self, name: str):
        self._maybe_reload()
        return getattr(object.__getattribute__(self, "_store"), name)
