from __future__ import annotations

import json as _json_mod
import logging
import os
import subprocess
import sys
import tempfile
import time

from fastmcp import FastMCP

from codespine.config import SETTINGS

_LOGGER = logging.getLogger(__name__)

from codespine import __version__
from codespine.analysis.community import detect_communities, symbol_community
from codespine.analysis.context import build_symbol_context
from codespine.analysis.coupling import get_coupling
from codespine.analysis.deadcode import detect_dead_code as detect_dead_code_analysis
from codespine.analysis.flow import trace_execution_flows as trace_flows_analysis
from codespine.analysis.impact import analyze_impact
from codespine.diff.branch_diff import compare_branches as compare_branches_analysis
from codespine.overlay.git_state import current_head
from codespine.overlay.merge import overlay_summary
from codespine.search.hybrid import hybrid_search
from codespine.watch.watcher import (
    clear_overlay as clear_overlay_state,
    get_overlay_status as get_overlay_status_state,
    promote_overlay as promote_overlay_state,
)
from codespine.cache.result_cache import ResultCache


def _json(data: dict) -> str:
    """Serialize response dict to a JSON string.

    FastMCP double-serialises dict return values on many transports (SSE,
    stdio) producing duplicate JSON payloads that waste ~50 K tokens/session.
    Returning a pre-serialised string guarantees a single TextContent block.

    Strips None values and empty containers to keep payloads compact.
    """
    cleaned = {k: v for k, v in data.items() if v is not None and v != [] and v != {}}
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
    except Exception:
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
        except Exception:
            pass
    return repo_path_provider()


def _no_symbols_response(note: str = "No symbols indexed. Run 'codespine analyse <path>' first.") -> str:
    return _json({"available": False, "note": note})


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


def _staleness_meta(
    store,
    response: dict,
    project: str | None = None,
    overlay_store=None,
    deep_scope: bool = False,
    compact: bool = True,
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
                    # Stale warning goes FIRST so LLMs see it immediately.
                    prefix["stale_warning"] = (
                        f"Index is {age // 3600}h {(age % 3600) // 60}m old. "
                        "Run analyse_project() or start_watch() to refresh."
                    )
                if not compact:
                    response["index_age_seconds"] = age
                    response["indexed_at_epoch"] = ts
    except Exception:
        pass

    if overlay_store is not None:
        try:
            summary = overlay_summary(overlay_store, project=project)
            if compact:
                # Only surface overlay info when there are actually dirty files.
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
                response["working_head_commit"] = current_head(meta.get("path") or response.get("path") or "")
            if deep_scope and summary.get("overlay_present"):
                response["overlay_excluded"] = True
                response["note"] = "Results reflect committed index only; uncommitted overlay changes are excluded."
        except Exception:
            pass

    # Merge prefix (stale warning first) with the rest of the response.
    final = {**prefix, **response}
    return _json(final)


class _StoreProxy:
    """Wraps a GraphStore and hot-reloads from the read replica when the
    post-analyse sentinel file is touched.

    After `codespine analyse` finishes it copies the write DB to
    ``~/.codespine_db_read`` and writes ``~/.codespine_db_read.updated``.
    This proxy checks that sentinel's mtime before every attribute access and
    silently swaps in a fresh read-only GraphStore so the MCP daemon picks up
    the new index without restarting.
    """

    def __init__(self, store) -> None:
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_sentinel", SETTINGS.db_snapshot_path + ".updated")
        object.__setattr__(self, "_last_mtime", self._sentinel_mtime())

    def _sentinel_mtime(self) -> float:
        try:
            return os.path.getmtime(object.__getattribute__(self, "_sentinel"))
        except FileNotFoundError:
            return 0.0

    def _maybe_reload(self) -> None:
        current = self._sentinel_mtime()
        if current > object.__getattribute__(self, "_last_mtime"):
            from codespine.db.store import GraphStore as _GS
            try:
                new_store = _GS(read_only=True)
                object.__setattr__(self, "_store", new_store)
                object.__setattr__(self, "_last_mtime", current)
                _LOGGER.info("MCP: hot-reloaded GraphStore from updated snapshot")
            except Exception as exc:
                _LOGGER.warning("MCP: hot-reload failed: %s", exc)

    def __getattr__(self, name: str):
        self._maybe_reload()
        return getattr(object.__getattribute__(self, "_store"), name)


def build_mcp_server(store, repo_path_provider):
    store = _StoreProxy(store)
    _raw_mcp = FastMCP("codespine")
    overlay_store = getattr(store, "overlay_store", None)

    # ── Anti-duplicate-JSON wrapper ────────────────────────────────────
    # FastMCP double-serialises dict return values on many transports,
    # producing duplicate JSON payloads that waste ~50 K tokens/session.
    # We intercept tool registration so every tool's dict return is
    # pre-serialised to a JSON string (single TextContent block).
    import functools as _functools

    class _JsonMCP:
        """Thin proxy that wraps tool functions to return JSON strings."""
        def __getattr__(self, name):
            return getattr(_raw_mcp, name)

        def tool(self, *args, **kwargs):
            original_decorator = _raw_mcp.tool(*args, **kwargs)
            def wrapper(fn):
                @_functools.wraps(fn)
                def json_fn(*a, **kw):
                    result = fn(*a, **kw)
                    if isinstance(result, dict):
                        return _json(result)
                    return result
                return original_decorator(json_fn)
            return wrapper

        def run(self):
            return _raw_mcp.run()

    mcp = _JsonMCP()

    # Background job state (per-server-instance, persists across tool calls)
    _watch: dict = {"proc": None, "path": None, "started_at": None, "interval": 30}
    _analyse: dict = {"proc": None, "path": None, "started_at": None, "log_path": None, "returncode": None}

    # Per-server result cache (FR-12): LRU cache keyed by (tool, args_hash, snapshot_mtime).
    # Invalidated automatically when the read replica sentinel file changes.
    _result_cache = ResultCache(maxsize=256, ttl_s=300.0)

    def _cache_key(tool_name: str, **kwargs):
        """Build a cache key using current snapshot mtime."""
        try:
            sentinel = getattr(store, "_snapshot_path", "") + ".updated"
            mtime = os.path.getmtime(sentinel) if os.path.exists(sentinel) else 0.0
        except OSError:
            mtime = 0.0
        return ResultCache.make_key(tool_name, kwargs, mtime)

    # FR-03: Auto-start watch if indexed projects exist and watch is not running.
    def _maybe_auto_start_watch() -> None:
        try:
            projs = store.query_records(
                "MATCH (p:Project) RETURN p.path as path, p.id as id ORDER BY p.indexed_at DESC LIMIT 1"
            )
            if not projs:
                return
            watch_path = projs[0].get("path", "")
            if not watch_path or not os.path.isdir(watch_path):
                return
            # Start watch as a background subprocess (same as start_watch tool).
            cmd = [sys.executable, "-m", "codespine.cli", "watch", watch_path,
                   "--interval", "30", "--allow-running"]
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _watch["proc"] = proc
            _watch["path"] = watch_path
            _watch["started_at"] = time.time()
            _LOGGER.info("Auto-started watch on %s (pid %d)", watch_path, proc.pid)
        except Exception as exc:
            _LOGGER.debug("Auto-watch skipped: %s", exc)

    # Trigger auto-watch in a daemon thread so server startup isn't delayed.
    import threading as _threading
    _auto_watch_thread = _threading.Thread(target=_maybe_auto_start_watch, daemon=True, name="codespine-auto-watch")
    _auto_watch_thread.start()

    # ------------------------------------------------------------------
    # Connectivity / feature discovery
    # ------------------------------------------------------------------

    @mcp.tool()
    def ping():
        """Verify the MCP server is alive. Call this first to confirm connectivity."""
        return _json({"status": "ok", "version": __version__})

    @mcp.tool()
    def get_capabilities():
        """
        Return what is indexed and which features are available RIGHT NOW.
        Call this before other tools so you know what's ready without trial-and-error.
        Features marked false may need 'codespine analyse --deep' or optional dependencies.
        """
        try:
            projects = store.query_records(
                """
                MATCH (p:Project)
                RETURN p.id as id,
                       p.path as path,
                       p.indexed_at as indexed_at,
                       p.indexed_commit as indexed_commit,
                       p.overlay_dirty as overlay_dirty
                """
            )
        except Exception:
            # Old DB schema (pre-0.4.0) doesn't have indexed_at column yet.
            # Falls back gracefully; column is added next time 'analyse' runs.
            projects = store.query_records(
                "MATCH (p:Project) RETURN p.id as id, p.path as path"
            )
        sym_q = store.query_records("MATCH (s:Symbol) RETURN count(s) as count")
        comm_q = store.query_records("MATCH (c:Community) RETURN count(c) as count")
        flow_q = store.query_records("MATCH (f:Flow) RETURN count(f) as count")
        coup_q = store.query_records("MATCH ()-[r:CO_CHANGED_WITH]->() RETURN count(r) as count")

        from codespine.search.vector import _load_model
        has_embeddings = _load_model() is not None

        # Check git availability on the default path AND on each indexed
        # project path.  Project-scoped git operations (git_log, git_diff,
        # compare_branches) work when the project path is a git repo, even
        # if the default path (cwd) is not.
        repo = repo_path_provider()
        git_ok = _git_available(repo)
        if not git_ok:
            for p in projects:
                pp = p.get("path", "")
                if pp and os.path.isdir(pp) and _git_available(pp):
                    git_ok = True
                    break

        n_sym = sym_q[0]["count"] if sym_q else 0
        n_comm = comm_q[0]["count"] if comm_q else 0
        n_flows = flow_q[0]["count"] if flow_q else 0
        n_coup = coup_q[0]["count"] if coup_q else 0

        # Check if any symbols have embeddings stored
        emb_q = store.query_records(
            "MATCH (s:Symbol) WHERE s.embedding IS NOT NULL RETURN count(s) as count"
        )
        has_stored_embeddings = (emb_q[0]["count"] if emb_q else 0) > 0

        watch_running = _watch["proc"] is not None and _watch["proc"].poll() is None
        analyse_running = _analyse["proc"] is not None and _analyse["proc"].poll() is None
        overlay_meta = overlay_summary(overlay_store) if overlay_store is not None else {}
        overlay_status = get_overlay_status_state(store) if overlay_store is not None else []

        now = int(time.time())
        stale_projects = []
        for p in projects:
            ts = int(p.get("indexed_at") or 0)
            if ts and (now - ts) > 3600 and not watch_running:
                age_h = (now - ts) // 3600
                stale_projects.append(f"{p['id']} ({age_h}h old)")

        notes: dict[str, str] = {}
        if stale_projects:
            notes["stale_index"] = (
                f"Index is stale for: {', '.join(stale_projects)}. "
                "Run analyse_project() or start_watch() to refresh."
            )
        if not n_comm:
            notes["community_detection"] = "Run 'codespine analyse --deep' to enable"
        if not n_flows:
            notes["execution_flows"] = "Run 'codespine analyse --deep' to enable"
        if not n_coup:
            notes["change_coupling"] = "Run 'codespine analyse --deep' to enable"
        if not has_embeddings:
            notes["semantic_embeddings"] = "Install 'codespine[ml]' for real vector search (hash fallback active)"
        if not has_stored_embeddings and n_sym > 0:
            notes["stored_embeddings"] = "Symbols indexed without embeddings. Rerun 'codespine analyse --embed' for semantic search."
        if not git_ok:
            notes["git_log"] = "Not a git repository, or git is not installed"
            notes["git_diff"] = "Not a git repository, or git is not installed"
        if not watch_running:
            notes["watch_mode"] = (
                "Watch mode is not active. Call start_watch(path) to enable real-time re-indexing. "
                "RECOMMENDED: start watch mode during active development."
            )

        # Detect unresolved imports → hint about unindexed sibling projects
        unresolved_imports: dict[str, list[str]] = {}
        try:
            from codespine.indexer.engine import JavaIndexer as _JI
            unresolved_imports = _JI.detect_unresolved_imports(store)
            if unresolved_imports:
                pkgs = list(unresolved_imports.keys())[:5]
                notes["unresolved_imports"] = (
                    f"Imports from unindexed packages detected: {', '.join(pkgs)}. "
                    "Consider indexing these projects for complete cross-project tracing."
                )
        except Exception:
            pass

        return {
            "available": True,
            "indexed_projects": projects,
            "symbol_count": n_sym,
            **overlay_meta,
            "features": {
                "ping": True,
                "list_projects": True,
                "search_hybrid": n_sym > 0,
                "get_impact": n_sym > 0,
                "get_symbol_context": n_sym > 0,
                "detect_dead_code": n_sym > 0,
                "trace_execution_flows": n_sym > 0,
                "community_detection": n_comm > 0,
                "execution_flows": n_flows > 0,
                "change_coupling": n_coup > 0,
                "semantic_embeddings": has_embeddings,
                "stored_embeddings": has_stored_embeddings,
                "git_log": git_ok,
                "git_diff": git_ok,
                "compare_branches": git_ok,
                "get_neighborhood": n_sym > 0,
                "reindex_file": True,
                "watch_mode": True,
                "analyse_project": True,
                "get_overlay_status": True,
                "promote_overlay": True,
                "clear_overlay": True,
                "force_reset_index": True,
            },
            "background_jobs": {
                "watch_running": watch_running,
                "watch_path": _watch["path"] if watch_running else None,
                "analyse_running": analyse_running,
                "analyse_path": _analyse["path"] if analyse_running else None,
            },
            "overlay_projects": overlay_status,
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Guide – static tool catalog + workflows for agents
    # ------------------------------------------------------------------

    @mcp.tool()
    def guide():
        """
        How to use CodeSpine: system overview, tool catalog, recommended
        workflows, and tips.  Call this FIRST if you have never used
        CodeSpine before.  For live index state (what is indexed right now),
        call get_capabilities() instead.
        """
        from codespine.guide import GUIDE_SECTIONS

        return _json({"version": __version__, "sections": GUIDE_SECTIONS})

    # ------------------------------------------------------------------
    # Project listing
    # ------------------------------------------------------------------

    @mcp.tool()
    def list_projects():
        """List all indexed projects with their symbol and file counts."""
        try:
            projects = store.query_records(
                "MATCH (p:Project) RETURN p.id as id, p.path as path, p.indexed_at as indexed_at"
            )
        except Exception:
            projects = store.query_records(
                "MATCH (p:Project) RETURN p.id as id, p.path as path"
            )
        if not projects:
            return {"available": False, "note": "No projects indexed yet. Run 'codespine analyse <path>'."}
        now = int(time.time())
        result = []
        for p in projects:
            sym = store.query_records(
                """
                MATCH (s:Symbol), (f:File)
                WHERE s.file_id = f.id AND f.project_id = $pid
                RETURN count(s) as count
                """,
                {"pid": p["id"]},
            )
            files = store.query_records(
                "MATCH (f:File) WHERE f.project_id = $pid RETURN count(f) as count",
                {"pid": p["id"]},
            )
            indexed_at_ts = int(p.get("indexed_at") or 0)
            age_s = now - indexed_at_ts if indexed_at_ts else None
            entry: dict = {
                "project_id": p["id"],
                "path": p["path"],
                "symbol_count": sym[0]["count"] if sym else 0,
                "file_count": files[0]["count"] if files else 0,
                "indexed_at_epoch": indexed_at_ts or None,
                "index_age_seconds": age_s,
            }
            if age_s is not None and age_s > 3600:
                entry["stale_warning"] = (
                    f"Index is {age_s // 3600}h {(age_s % 3600) // 60}m old. "
                    "Run analyse_project() or start_watch() to refresh."
                )
            result.append(entry)
        return {"available": True, "projects": result}

    # ------------------------------------------------------------------
    # Search & analysis (all support optional project scoping)
    # ------------------------------------------------------------------

    @mcp.tool()
    def search_hybrid(query: str, k: int = 20, project: str | None = None):
        """
        Hybrid symbol search (BM25 + vector + fuzzy, fused with RRF).
        Pass project=<project_id> to scope results to a single indexed project.
        Use list_projects to see available project IDs.
        """
        results = hybrid_search(store, query, k=k, project=project)
        if not results:
            return _no_symbols_response()
        return _staleness_meta(store, {"available": True, "results": results}, project, overlay_store=overlay_store)

    @mcp.tool()
    def get_impact(symbol: str, max_depth: int = 4, project: str | None = None):
        """
        Caller-tree impact analysis for a symbol.

        Returns two sections:
          resolved_to     — the symbol(s) matched by name
          impacted_callers — BFS caller groups by depth (1 = direct, 2 = indirect, 3+ = transitive)
          self_callers    — methods in the same class that call the target (separated for clarity)

        Includes DI edges (@Inject/@Autowired/@Provides/@Bean) when the index has been
        built with a DI-aware version of CodeSpine.

        project scopes the target symbol lookup; cross-project callers are always included.
        """
        try:
            _ck = _cache_key("get_impact", symbol=symbol, max_depth=max_depth, project=project)
            _cached = _result_cache.get(_ck)
            if _cached is not None:
                return _cached
            normalized = _normalize_symbol_input(symbol)
            result = analyze_impact(store, normalized, max_depth=max_depth, project=project)
            if not result.get("resolved_to"):
                # Retry with the raw input in case the ID matched exactly.
                result = analyze_impact(store, symbol, max_depth=max_depth, project=project)
            if not result.get("resolved_to"):
                return {"available": False, "note": f"Symbol '{symbol}' not found in the index."}
            out = _staleness_meta(store, {"available": True, **result}, project, overlay_store=overlay_store)
            _result_cache.put(_ck, out)
            return out
        except Exception as exc:
            return _safe_tool_response("get_impact", exc)

    @mcp.tool()
    def detect_dead_code(limit: int = 200, project: str | None = None, strict: bool = False):
        """
        Detect methods with no incoming calls (after Java-aware exemptions).
        Pass project to scope to a single module.

        Parameters:
          strict – When True, only main()/@Test and explicit entry-point
                   annotations are exempted. Constructors, getters/setters,
                   contract methods (toString, hashCode, equals), and method
                   overrides are NOT exempt. Use this for a thorough audit.
                   Each result includes a confidence level (high/medium/low):
                     high   = private method, almost certainly dead
                     medium = package-private or protected
                     low    = public method, could be called via reflection

        Returns dead_code list, count, and an exemption_stats dict showing
        how many candidates were found and how many were filtered out by the
        exemption rules — useful for validating that the feature is working
        even when the dead list is empty.
        """
        _ck = _cache_key("detect_dead_code", limit=limit, project=project, strict=strict)
        _cached = _result_cache.get(_ck)
        if _cached is not None:
            return _cached

        raw = detect_dead_code_analysis(store, limit=limit, project=project, strict=strict)
        if raw is None:
            return _no_symbols_response()

        # Separate the sentinel stats entry appended by the analysis function.
        stats: dict = {}
        dead = []
        for entry in raw:
            if "_stats" in entry:
                stats = entry["_stats"]
            else:
                dead.append(entry)

        out = _staleness_meta(store, {
            "available": True,
            "dead_code": dead,
            "count": len(dead),
            "exemption_stats": stats,
        }, project, overlay_store=overlay_store, deep_scope=True)
        _result_cache.put(_ck, out)
        return out

    @mcp.tool()
    def trace_execution_flows(entry_symbol: str | None = None, max_depth: int = 6, project: str | None = None):
        """
        Trace execution flows from entry points (main methods, tests).
        Pass project to scope entry-point discovery to a single module.
        """
        if entry_symbol:
            entry_symbol = _normalize_symbol_input(entry_symbol)
        flows = trace_flows_analysis(store, entry_symbol=entry_symbol, max_depth=max_depth, project=project)
        if not flows:
            return _no_symbols_response("No entry points found. Run 'codespine analyse --deep' or provide entry_symbol.")
        return _staleness_meta(store, {"available": True, "flows": flows}, project, overlay_store=overlay_store, deep_scope=True)

    @mcp.tool()
    def get_symbol_community(symbol: str):
        """Return the architectural community cluster a symbol belongs to."""
        # NOTE: do NOT call detect_communities() here — the MCP server opens the
        # graph DB read-only, so any write attempt raises "Cannot execute write
        # operations in a read-only database!".  Communities are computed once
        # during 'codespine analyse --deep' and persisted; we just read them.
        normalized = _normalize_symbol_input(symbol)
        result = symbol_community(store, normalized)
        if not result.get("matches"):
            result = symbol_community(store, symbol)
        if not result.get("matches"):
            return {"available": False, "note": "No community data yet. Run 'codespine analyse --deep'."}
        return _staleness_meta(store, {"available": True, **result}, overlay_store=overlay_store, deep_scope=True)

    @mcp.tool()
    def get_change_coupling(
        symbol: str | None = None,
        days: int = 5,
        min_strength: float = 0.3,
        min_cochanges: int = 3,
    ):
        """
        Files that changed together in the last N days (git co-change coupling).
        Requires 'codespine analyse --deep' to have been run.
        """
        result = get_coupling(store, symbol=symbol, days=days, min_strength=min_strength, min_cochanges=min_cochanges)
        if not result:
            return {
                "available": False,
                "note": "No coupling data. Run 'codespine analyse --deep' with a git repository.",
            }
        return _staleness_meta(store, {"available": True, "coupling": result}, overlay_store=overlay_store, deep_scope=True)

    @mcp.tool()
    def find_injections(symbol: str, project: str | None = None):
        """
        Find all dependency-injection bindings for a class symbol.

        Returns:
          injected_by   — classes that @Inject / @Autowired this class (consumers)
          provides_for  — @Provides / @Bean methods that return this type
          binds_to      — interface bindings (BINDS_INTERFACE edges) where this class
                          is the implementation or the interface

        Requires the index to have been built with DI-aware indexing (v0.9+).
        If empty, try re-indexing with 'codespine analyse'.
        """
        try:
            name_lower = symbol.lower()
            # Resolve symbol → class IDs.
            class_recs = store.query_records(
                """
                MATCH (c:Class)
                WHERE lower(c.name) = $namel OR lower(c.fqcn) CONTAINS $namel
                RETURN c.id as id, c.name as name, c.fqcn as fqcn
                LIMIT 10
                """,
                {"namel": name_lower},
            )
            if not class_recs:
                return {"available": False, "note": f"Class '{symbol}' not found in the index."}

            all_injected_by: list[dict] = []
            all_provides_for: list[dict] = []
            all_binds_to: list[dict] = []

            for cls_rec in class_recs:
                cid = cls_rec["id"]
                proj_clause = "AND f.project_id = $proj" if project else ""
                proj_params: dict = {"cid": cid}
                if project:
                    proj_params["proj"] = project

                # Who injects this class?
                try:
                    inj = store.query_records(
                        f"""
                        MATCH (a:Class)-[r:INJECTS]->(b:Class {{id: $cid}}), (f:File)
                        WHERE a.file_id = f.id {proj_clause}
                        RETURN a.fqcn as injector, r.framework as framework,
                               r.binding_type as binding_type, r.confidence as confidence
                        """,
                        proj_params,
                    )
                    all_injected_by.extend(inj)
                except Exception:
                    pass

                # What does this class provide?
                try:
                    prov = store.query_records(
                        f"""
                        MATCH (a:Class {{id: $cid}})-[r:INJECTS]->(b:Class), (f:File)
                        WHERE a.file_id = f.id {proj_clause}
                        RETURN b.fqcn as provided_type, r.framework as framework,
                               r.binding_type as binding_type, r.confidence as confidence
                        """,
                        proj_params,
                    )
                    all_provides_for.extend(prov)
                except Exception:
                    pass

                # Interface bindings.
                try:
                    binds = store.query_records(
                        """
                        MATCH (a:Class)-[r:BINDS_INTERFACE]->(b:Class)
                        WHERE a.id = $cid OR b.id = $cid
                        RETURN a.fqcn as impl_class, b.fqcn as interface_class,
                               r.confidence as confidence, r.reason as reason
                        """,
                        {"cid": cid},
                    )
                    all_binds_to.extend(binds)
                except Exception:
                    pass

            return _staleness_meta(store, {
                "available": True,
                "symbol": symbol,
                "matched_classes": [{"name": r["name"], "fqcn": r["fqcn"]} for r in class_recs],
                "injected_by": all_injected_by,
                "provides_for": all_provides_for,
                "binds_to": all_binds_to,
                "note": (
                    "Empty results mean either no DI bindings exist or the index was built "
                    "before DI-aware indexing (v0.9+). Re-run 'codespine analyse' to update."
                    if not (all_injected_by or all_provides_for or all_binds_to) else None
                ),
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("find_injections", exc)

    @mcp.tool()
    def get_symbol_context(query: str, max_depth: int = 3, project: str | None = None):
        """
        One-shot deep context for a symbol: search + impact + community + flows.
        Pass project to scope the search to a single indexed module.
        """
        result = build_symbol_context(store, query, max_depth=max_depth, project=project)
        if not result.get("search_candidates"):
            return _no_symbols_response()
        return _staleness_meta(store, {"available": True, **result}, project, overlay_store=overlay_store)

    @mcp.tool()
    def get_codebase_stats():
        """
        Per-project and aggregate stats: files, classes, methods, call edges, embeddings.

        Use this to understand the size and coverage of each indexed project before
        deciding which project= scope to pass to analysis tools.
        """
        projects = store.query_records(
            """
            MATCH (p:Project)
            RETURN p.id as id, p.path as path, p.indexed_commit as indexed_commit, p.overlay_dirty as overlay_dirty
            ORDER BY p.id
            """
        )
        if not projects:
            return {"available": False, "note": "No projects indexed yet. Run 'codespine analyse <path>'."}

        per_project = []
        total_files = total_classes = total_methods = total_calls = total_emb = 0
        for p in projects:
            pid = p["id"]
            files = store.query_records(
                "MATCH (f:File) WHERE f.project_id = $pid RETURN count(f) as n", {"pid": pid}
            )
            classes = store.query_records(
                "MATCH (c:Class), (f:File) WHERE c.file_id = f.id AND f.project_id = $pid RETURN count(c) as n",
                {"pid": pid},
            )
            methods = store.query_records(
                "MATCH (m:Method), (c:Class), (f:File) WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid RETURN count(m) as n",
                {"pid": pid},
            )
            calls = store.query_records(
                "MATCH (ma:Method)-[:CALLS]->(mb:Method), (ca:Class), (fa:File) WHERE ma.class_id = ca.id AND ca.file_id = fa.id AND fa.project_id = $pid RETURN count(*) as n",
                {"pid": pid},
            )
            emb = store.query_records(
                "MATCH (s:Symbol), (f:File) WHERE s.file_id = f.id AND f.project_id = $pid AND s.embedding IS NOT NULL RETURN count(s) as n",
                {"pid": pid},
            )
            n_files = files[0]["n"] if files else 0
            n_classes = classes[0]["n"] if classes else 0
            n_methods = methods[0]["n"] if methods else 0
            n_calls = calls[0]["n"] if calls else 0
            n_emb = emb[0]["n"] if emb else 0
            per_project.append({
                "project_id": pid,
                "path": p["path"],
                "files": n_files,
                "classes": n_classes,
                "methods": n_methods,
                "calls_out": n_calls,
                "embeddings": n_emb,
                "indexed_commit": p.get("indexed_commit", ""),
                "overlay_dirty": bool(p.get("overlay_dirty", False)),
            })
            total_files += n_files
            total_classes += n_classes
            total_methods += n_methods
            total_calls += n_calls
            total_emb += n_emb

        return _staleness_meta(store, {
            "available": True,
            "per_project": per_project,
            "totals": {
                "projects": len(projects),
                "files": total_files,
                "classes": total_classes,
                "methods": total_methods,
                "calls": total_calls,
                "embeddings": total_emb,
            },
        }, overlay_store=overlay_store)

    # ------------------------------------------------------------------
    # Ambiguity resolution + structural exploration
    # ------------------------------------------------------------------

    @mcp.tool()
    def find_symbol(
        name: str,
        kind: str | None = None,
        project: str | None = None,
        limit: int = 50,
    ):
        """
        Exact / prefix name lookup – returns ALL matching symbols across every project.

        Use this to resolve ambiguity when a name appears in multiple projects or
        packages. Unlike search_hybrid (which ranks by relevance), find_symbol
        returns every match so you can inspect the full set and pick the right one.

        Parameters:
          name    – Simple class/method name, fully-qualified name, or prefix.
                    Matching is case-insensitive on the simple name; exact on the FQCN.
          kind    – Optional filter: "class", "method", or "field".
          project – Optional project_id to restrict the search.
          limit   – Max results per kind (default 50).

        Returns results grouped by kind and project, each with:
          id, name, fqname, project_id, file_path, line, col.
        """
        name_lower = name.lower()
        project_clause = "AND f.project_id = $proj" if project else ""
        # Note: only $namel and $lim are referenced in the queries below.
        # Do NOT add extra keys here — some Kuzu versions raise "Parameter not found"
        # when the params dict contains keys absent from the query string.
        params: dict = {"namel": name_lower, "lim": limit}
        if project:
            params["proj"] = project

        from codespine.overlay.merge import merged_class_records, merged_method_records

        classes: list[dict] = []
        methods: list[dict] = []
        if kind != "method":
            for rec in merged_class_records(store, overlay_store, project=project):
                rec_name = str(rec.get("name") or "").lower()
                rec_fqcn = str(rec.get("fqcn") or "").lower()
                if rec_name == name_lower or rec_fqcn == name_lower or name_lower in rec_fqcn or name_lower in rec_name:
                    classes.append(
                        {
                            "id": rec.get("id"),
                            "name": rec.get("name"),
                            "fqname": rec.get("fqcn"),
                            "package": rec.get("package"),
                            "project_id": rec.get("project_id"),
                            "file_path": rec.get("file_path"),
                        }
                    )
                    if len(classes) >= limit:
                        break

        if kind != "class":
            for rec in merged_method_records(store, overlay_store, project=project):
                rec_name = str(rec.get("name") or "").lower()
                signature = str(rec.get("signature") or "").lower()
                if rec_name == name_lower or name_lower in signature:
                    methods.append(
                        {
                            "id": rec.get("id"),
                            "name": rec.get("name"),
                            "fqname": rec.get("signature"),
                            "class_fqcn": rec.get("class_fqcn"),
                            "project_id": rec.get("project_id"),
                            "file_path": rec.get("file_path"),
                            "return_type": rec.get("return_type"),
                        }
                    )
                    if len(methods) >= limit:
                        break

        fields: list[dict] = []
        if kind in (None, "field"):
            project_clause_f = "AND f.project_id = $proj" if project else ""
            field_params: dict = {"namel": name_lower, "lim": limit}
            if project:
                field_params["proj"] = project
            field_recs = store.query_records(
                f"""
                MATCH (s:Symbol), (f:File)
                WHERE s.file_id = f.id AND s.kind = 'field'
                  AND (lower(s.name) = $namel OR lower(s.fqname) CONTAINS $namel)
                  {project_clause_f}
                RETURN s.id as id, s.name as name, s.fqname as fqname,
                       f.project_id as project_id, f.path as file_path,
                       s.line as line, s.col as col
                LIMIT $lim
                """,
                field_params,
            )
            for rec in field_recs:
                fields.append(
                    {
                        "id": rec.get("id"),
                        "name": rec.get("name"),
                        "fqname": rec.get("fqname"),
                        "project_id": rec.get("project_id"),
                        "file_path": rec.get("file_path"),
                        "line": rec.get("line"),
                        "col": rec.get("col"),
                    }
                )

        total = len(classes) + len(methods) + len(fields)
        if total == 0:
            return {
                "available": False,
                "note": f"No symbols found matching '{name}'. Try a shorter prefix or use search_hybrid for fuzzy matching.",
            }

        # FR-05: Smart disambiguation — when the query exactly matches a class name,
        # mark it as primary_match and sort: class > constructor > other methods > fields.
        exact_class_names = {c["name"].lower() for c in classes if c.get("name") and c["name"].lower() == name_lower}
        if exact_class_names:
            for c in classes:
                if c.get("name", "").lower() == name_lower:
                    c["primary_match"] = True
            # Sort methods so constructors (same name as class) come first.
            methods.sort(key=lambda m: (0 if m.get("name", "").lower() in exact_class_names else 1, m.get("name", "")))

        # Group by project_id so agents can see the landscape at a glance
        by_project: dict[str, dict] = {}
        for c in classes:
            pid = c.get("project_id", "?")
            by_project.setdefault(pid, {"classes": [], "methods": [], "fields": []})
            by_project[pid]["classes"].append(c)
        for m in methods:
            pid = m.get("project_id", "?")
            by_project.setdefault(pid, {"classes": [], "methods": [], "fields": []})
            by_project[pid]["methods"].append(m)
        for f in fields:
            pid = f.get("project_id", "?")
            by_project.setdefault(pid, {"classes": [], "methods": [], "fields": []})
            by_project[pid]["fields"].append(f)

        return _staleness_meta(store, {
            "available": True,
            "query": name,
            "total_matches": total,
            "by_project": by_project,
            "note": (
                f"Found {total} match(es). If multiple projects contain the same name, "
                "pass project=<project_id> to subsequent tools to avoid cross-project ambiguity."
            ) if total > 1 else None,
        }, project, overlay_store=overlay_store)

    @mcp.tool()
    def get_overlay_status(project: str | None = None):
        """Report uncommitted overlay state by project/module."""
        return _staleness_meta(
            store,
            {"available": True, "overlay": get_overlay_status_state(store, project=project)},
            project,
            overlay_store=overlay_store,
        )

    @mcp.tool()
    def promote_overlay(project: str | None = None):
        """Promote dirty overlay state into the committed base index immediately."""
        result = promote_overlay_state(store, project=project, require_head_change=False)
        return _staleness_meta(
            store,
            {"available": True, "promoted": result},
            project,
            overlay_store=overlay_store,
        )

    @mcp.tool()
    def clear_overlay(project: str | None = None):
        """Discard dirty overlay state without changing the committed base index."""
        result = clear_overlay_state(store, project=project)
        return _staleness_meta(
            store,
            {"available": True, "cleared": result},
            project,
            overlay_store=overlay_store,
        )

    @mcp.tool()
    def list_packages(project: str | None = None, limit: int = 200):
        """
        List all Java packages in the index, optionally scoped to one project.

        Use this to explore the structural layout of a codebase before searching.
        Returns each package with the project it belongs to and a class count,
        sorted by project then package.

        Tip: when you have multiple projects with overlapping package names (e.g.
        both have 'com.example.service'), pass project= to the other tools to avoid
        mixing results from different codebases.
        """
        project_clause = "AND f.project_id = $proj" if project else ""
        params: dict = {"lim": limit}
        if project:
            params["proj"] = project

        recs = store.query_records(
            f"""
            MATCH (c:Class), (f:File)
            WHERE c.file_id = f.id {project_clause}
            RETURN c.package as package, f.project_id as project_id, count(c) as class_count
            ORDER BY project_id, package
            LIMIT $lim
            """,
            params,
        )
        if not recs:
            return _no_symbols_response("No packages found. Run 'codespine analyse <path>' first.")

        # Group by project
        by_project: dict[str, list[dict]] = {}
        for r in recs:
            pid = r.get("project_id", "?")
            by_project.setdefault(pid, [])
            by_project[pid].append({
                "package": r.get("package") or "(default)",
                "class_count": r.get("class_count", 0),
            })

        return _staleness_meta(store, {
            "available": True,
            "total_packages": len(recs),
            "by_project": by_project,
        }, project, overlay_store=overlay_store)

    # ------------------------------------------------------------------
    # Git tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def git_log(file_path: str | None = None, limit: int = 20, project: str | None = None):
        """
        Recent git commits for the project (or a specific file).
        Returns available=false if the directory is not a git repository.
        Use project=<project_id> to target a specific indexed module's repo.
        TIP: Always pass project= to ensure the correct repo is used.
        """
        repo = _resolve_repo_path(store, project, repo_path_provider)
        if not os.path.isdir(repo):
            return {
                "available": False,
                "note": f"Path does not exist: {repo}. Pass project=<project_id> to resolve the repo from the index.",
            }
        if not _git_available(repo):
            return {
                "available": False,
                "note": (
                    f"Not a git repository at {repo}. "
                    "Pass project=<project_id> so the tool resolves the correct repo root. "
                    "Use list_projects() to see available IDs."
                ),
            }
        cmd = ["git", "log", f"--max-count={limit}", "--oneline", "--no-decorate"]
        if file_path:
            cmd += ["--", file_path]
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {"available": False, "error": r.stderr.strip(), "repo_path": repo}
        log_lines = r.stdout.strip().splitlines()
        return {
            "available": True,
            "project": project or repo,
            "repo_path": repo,
            "log": log_lines,
            "note": f"{len(log_lines)} commit(s)" + (" (no commits yet)" if not log_lines else ""),
        }

    @mcp.tool()
    def git_diff(ref: str = "HEAD", file_path: str | None = None, project: str | None = None):
        """
        Show git diff (working tree vs ref, or between two refs separated by '...').
        Output is truncated to 200 lines.
        Returns available=false if the directory is not a git repository.
        TIP: Always pass project= to ensure the correct repo is used.
        """
        repo = _resolve_repo_path(store, project, repo_path_provider)
        if not os.path.isdir(repo):
            return {
                "available": False,
                "note": f"Path does not exist: {repo}. Pass project=<project_id> to resolve the repo from the index.",
            }
        if not _git_available(repo):
            return {
                "available": False,
                "note": (
                    f"Not a git repository at {repo}. "
                    "Pass project=<project_id> so the tool resolves the correct repo root. "
                    "Use list_projects() to see available IDs."
                ),
            }
        cmd = ["git", "diff", ref]
        if file_path:
            cmd += ["--", file_path]
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {"available": False, "error": r.stderr.strip(), "repo_path": repo}
        lines = r.stdout.splitlines()
        truncated = False
        if len(lines) > 200:
            lines = lines[:200]
            truncated = True
        diff_text = "\n".join(lines)
        return {
            "available": True,
            "project": project or repo,
            "repo_path": repo,
            "diff": diff_text,
            "truncated": truncated,
            "note": f"{len(lines)} line(s)" + (" — no changes" if not diff_text.strip() else ""),
        }

    @mcp.tool()
    def compare_branches(base_ref: str, head_ref: str, project: str | None = None):
        """
        Symbol-level diff between two git refs (branches, tags, commits).
        Pass project=<project_id> so the tool can resolve the correct git
        repository root from the indexed project path rather than relying on
        the MCP server's working directory (which may point to the graph DB
        location, not the source tree).
        """
        repo = _resolve_repo_path(store, project, repo_path_provider)
        if not _git_available(repo):
            return {
                "available": False,
                "note": (
                    "Not a git repository (or git not installed). "
                    "Pass project=<project_id> so the tool can resolve the repo "
                    "from the indexed project path. Use list_projects() to see available IDs."
                ),
            }
        result = compare_branches_analysis(repo, base_ref, head_ref)
        return {"available": True, **result}

    # ------------------------------------------------------------------
    # Watch mode tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def start_watch(
        path: str,
        global_interval: int = 30,
        overlay_debounce_ms: int = 1500,
        promote_on_commit: bool = True,
        install_hook: bool = False,
    ):
        """
        Start watching a project directory for Java file changes.

        Watch mode monitors .java files for changes and writes them DIRECTLY to the
        graph database (no intermediate overlay JSON).  Changes are immediately
        queryable after each file save.  When HEAD changes (on git commit), only the
        files that changed between commits are re-indexed — typically <1 second.

        Parameters:
          path               – Project directory to watch.
          global_interval    – Fallback poll interval in seconds (default 30; commit
                               detection uses a faster 5 s cycle regardless).
          overlay_debounce_ms – Debounce delay for file-save events (default 1500 ms).
          promote_on_commit  – Re-index changed files on each git commit (default True).
          install_hook       – If True, also install a git post-commit hook in the
                               project's .git/hooks/ so commits trigger re-indexing
                               even when watch mode is not running (opt-in).

        Returns the PID of the background watch process and the path being watched.
        Use get_watch_status() to check if it's still running.
        Use stop_watch() to stop it.
        """
        import os

        # Stop any previous watch process first
        if _watch["proc"] is not None and _watch["proc"].poll() is None:
            return {
                "available": True,
                "running": True,
                "path": _watch["path"],
                "pid": _watch["proc"].pid,
                "note": "Watch mode already running. Call stop_watch() first to restart on a different path.",
            }

        abs_path = os.path.abspath(path)
        if not os.path.isdir(abs_path):
            return {"available": False, "note": f"Path does not exist or is not a directory: {abs_path}"}

        # Optionally install the git post-commit hook.
        hook_status: dict = {}
        if install_hook:
            try:
                from codespine.watch.git_hook import install_post_commit_hook
                hook_file = install_post_commit_hook(abs_path)
                hook_status = {"hook_installed": True, "hook_path": hook_file}
            except Exception as exc:
                hook_status = {"hook_installed": False, "hook_error": str(exc)}

        import tempfile as _tempfile
        watch_err_file = _tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", prefix="codespine_watch_", delete=False
        )
        watch_err_path = watch_err_file.name
        watch_err_file.close()

        cmd = [
            sys.executable, "-m", "codespine.cli",
            "watch", "--path", abs_path,
            "--global-interval", str(global_interval),
            "--overlay-debounce-ms", str(overlay_debounce_ms),
        ]
        if not promote_on_commit:
            cmd.append("--no-promote-on-commit")

        proc = subprocess.Popen(
            cmd,
            stdout=open(watch_err_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )

        # Brief health check — if the process dies within 1 s it crashed at startup.
        time.sleep(1)
        if proc.poll() is not None:
            try:
                with open(watch_err_path, "r", encoding="utf-8", errors="replace") as fh:
                    err_tail = fh.read().strip().splitlines()[-10:]
            except Exception:
                err_tail = []
            return {
                "available": False,
                "note": (
                    f"Watch mode process exited immediately (code {proc.returncode}). "
                    "Check that the path is valid and watchfiles is installed."
                ),
                "error_tail": err_tail,
            }

        _watch["proc"] = proc
        _watch["path"] = abs_path
        _watch["started_at"] = time.time()
        _watch["interval"] = global_interval

        return {
            "available": True,
            "running": True,
            "path": abs_path,
            "pid": proc.pid,
            "global_interval_s": global_interval,
            "overlay_debounce_ms": overlay_debounce_ms,
            "promote_on_commit": promote_on_commit,
            **hook_status,
            "note": (
                "Watch mode started. File saves are written directly to the graph index and "
                "are immediately queryable. Commits trigger targeted re-indexing of changed files."
            ),
        }

    @mcp.tool()
    def stop_watch():
        """Stop the background watch mode process."""
        import signal as _signal

        proc = _watch.get("proc")
        if proc is None or proc.poll() is not None:
            _watch["proc"] = None
            _watch["path"] = None
            return {"available": True, "running": False, "note": "Watch mode was not running."}

        path = _watch["path"]
        try:
            proc.send_signal(_signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _watch["proc"] = None
        _watch["path"] = None
        _watch["started_at"] = None
        return {"available": True, "running": False, "stopped_path": path}

    @mcp.tool()
    def get_watch_status():
        """Get the current status of watch mode (running/stopped, path, uptime)."""
        proc = _watch.get("proc")
        running = proc is not None and proc.poll() is None
        result: dict = {"available": True, "running": running}
        if running:
            result["path"] = _watch["path"]
            result["pid"] = proc.pid
            result["global_interval_s"] = _watch.get("interval", 30)
            started = _watch.get("started_at")
            if started:
                result["uptime_s"] = round(time.time() - started)
        else:
            result["note"] = (
                "Watch mode is not running. Call start_watch(path) to enable real-time re-indexing. "
                "RECOMMENDED during active development."
            )
        return result

    # ------------------------------------------------------------------
    # Analysis trigger tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def analyse_project(
        path: str,
        full: bool = False,
        deep: bool = False,
        embed: bool = False,
    ):
        """
        Trigger indexing of a Java project (or workspace) as a background job.

        This starts 'codespine analyse' in a subprocess and returns immediately.
        Use get_analyse_status() to poll progress and completion.

        Parameters:
          path  – Absolute or relative path to the project/workspace to index.
          full  – If True, re-index every file even if unchanged (default: incremental).
          deep  – If True, also run community detection, flows, and coupling (slower).
          embed – If True, generate vector embeddings for semantic search (slow when
                  sentence-transformers is installed; BM25/fuzzy search works without them).

        RECOMMENDATION: Run without embed=True first for a fast initial index (<1 min).
        Add --embed later if you need semantic similarity search.
        """
        import os

        # If already running an analysis, report status instead of starting another
        if _analyse["proc"] is not None and _analyse["proc"].poll() is None:
            return {
                "available": True,
                "running": True,
                "path": _analyse["path"],
                "note": "Analysis already running. Call get_analyse_status() to check progress.",
            }

        abs_path = os.path.abspath(path)
        if not os.path.isdir(abs_path):
            return {"available": False, "note": f"Path does not exist or is not a directory: {abs_path}"}

        cmd = [sys.executable, "-m", "codespine.cli", "analyse", abs_path, "--allow-running"]
        if full:
            cmd.append("--full")
        else:
            cmd.append("--incremental")
        if deep:
            cmd.append("--deep")
        if embed:
            cmd.append("--embed")
        else:
            cmd.append("--no-embed")

        # Capture output to a temp file for progress inspection
        log_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", prefix="codespine_analyse_", delete=False
        )
        log_path = log_file.name
        log_file.close()

        proc = subprocess.Popen(
            cmd,
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
        )
        _analyse["proc"] = proc
        _analyse["path"] = abs_path
        _analyse["started_at"] = time.time()
        _analyse["log_path"] = log_path
        _analyse["returncode"] = None

        embed_note = " (with embeddings)" if embed else " (no embeddings – fast)"
        deep_note = " + deep analyses" if deep else ""
        return {
            "available": True,
            "running": True,
            "path": abs_path,
            "pid": proc.pid,
            "log_path": log_path,
            "note": (
                f"Analysis started{embed_note}{deep_note}. "
                "Call get_analyse_status() to check progress. "
                "Results will be available in the index as soon as the job completes."
            ),
        }

    @mcp.tool()
    def get_analyse_status():
        """
        Get the status of the current or most recent background analysis job.

        Returns running=True while analysis is in progress.
        Returns running=False with elapsed_s and tail of output when done.
        """
        proc = _analyse.get("proc")
        if proc is None:
            return {
                "available": True,
                "running": False,
                "note": "No analysis has been started this session. Call analyse_project(path) to begin.",
            }

        rc = proc.poll()
        running = rc is None
        started = _analyse.get("started_at") or time.time()
        elapsed = round(time.time() - started)

        result: dict = {
            "available": True,
            "running": running,
            "path": _analyse["path"],
            "elapsed_s": elapsed,
        }

        if not running:
            result["returncode"] = rc
            result["success"] = rc == 0

        # Read last 30 lines of output log for context
        log_path = _analyse.get("log_path")
        if log_path:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
                result["output_tail"] = lines[-30:] if len(lines) > 30 else lines
            except Exception:
                pass

        if running:
            result["note"] = f"Analysis in progress ({elapsed}s elapsed). Call get_analyse_status() again to check."
        elif rc == 0:
            result["note"] = f"Analysis completed successfully in {elapsed}s. Index is now updated."
        else:
            result["note"] = f"Analysis exited with code {rc} after {elapsed}s. Check output_tail for errors."

        return result

    # ------------------------------------------------------------------
    # Index reset tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def reset_project(project_id: str):
        """
        Remove all indexed data for a single project – clean slate for that project.

        Deletes the project's files, classes, methods, symbols, and the Project
        node itself from the graph. The meta cache is also cleared.

        This is different from watch mode (which tracks live changes) – reset
        removes everything so you can re-index from scratch with analyse_project().

        Typical workflow:
          1. reset_project("my-app")
          2. analyse_project("/path/to/my-app", full=True)

        Returns the project path that was cleared (for confirmation).
        """
        import os as _os

        # Look up path before clearing so we can return it and suggest re-indexing
        recs = store.query_records(
            "MATCH (p:Project) WHERE p.id = $pid RETURN p.path as path LIMIT 1",
            {"pid": project_id},
        )
        if not recs:
            return {
                "available": False,
                "note": f"Project '{project_id}' not found. Use list_projects() to see available project IDs.",
            }
        project_path = recs[0].get("path", "")

        proc = subprocess.run(
            [sys.executable, "-m", "codespine.cli", "clear-project", project_id, "--allow-running"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            error_text = (proc.stderr.strip() or proc.stdout.strip())[:300]
            return {
                "available": False,
                "note": (
                    f"Reset failed: {error_text}. "
                    "If this is a buffer pool or DB corruption error, use force_reset_index() instead."
                ),
            }
        return {
            "available": True,
            "cleared_project": project_id,
            "path": project_path,
            "note": (
                f"Project '{project_id}' has been cleared from the index. "
                f"Call analyse_project('{project_path}', full=True) to re-index from scratch."
            ),
        }

    @mcp.tool()
    def reset_index():
        """
        Remove ALL indexed data – complete clean slate across every project.

        Deletes every project, file, class, method, symbol, community, and flow
        from the graph. The database file itself is kept so the MCP server remains
        usable without a restart.

        This is a destructive but fast operation. After calling this, no projects
        will be indexed until you run analyse_project() again for each one.

        Typical workflow:
          1. reset_index()
          2. analyse_project("/path/to/project-a")
          3. analyse_project("/path/to/project-b")
        """
        # Capture the list of projects before clearing so we can report them
        projects = store.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path")

        proc = subprocess.run(
            [sys.executable, "-m", "codespine.cli", "clear-index", "--allow-running"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            error_text = (proc.stderr.strip() or proc.stdout.strip())[:300]
            return {
                "available": False,
                "note": (
                    f"Reset failed: {error_text}. "
                    "If this is a buffer pool or DB corruption error, use force_reset_index() instead."
                ),
            }

        cleared = [{"project_id": p["id"], "path": p["path"]} for p in projects]
        paths = [p["path"] for p in projects]
        re_index_hint = " ".join(f"analyse_project('{p}')" for p in paths[:3])
        if len(paths) > 3:
            re_index_hint += f" ... ({len(paths) - 3} more)"

        return {
            "available": True,
            "cleared_count": len(cleared),
            "cleared_projects": cleared,
            "note": (
                f"Index cleared. {len(cleared)} project(s) removed. "
                f"Re-index with: {re_index_hint}" if paths else
                "Index cleared. No projects were indexed."
            ),
        }

    @mcp.tool()
    def force_reset_index():
        """
        Emergency reset: delete ALL CodeSpine data files without touching the
        DB engine.

        Use this when the buffer pool is exhausted and normal reset/clear
        commands also fail with OOM errors.  This bypasses Kuzu entirely by
        removing all data files from disk.

        After calling this, restart the MCP server and re-index all projects
        with analyse_project().

        This is the nuclear option — only use when reset_project() and
        reset_index() fail with buffer pool errors.
        """
        from codespine.db.store import GraphStore as _GS

        removed = _GS.force_delete_all_data()
        return {
            "available": True,
            "removed_paths": removed,
            "removed_count": len(removed),
            "note": (
                f"Force-reset complete. {len(removed)} path(s) removed. "
                "Restart the MCP server (codespine stop && codespine start) "
                "and re-index projects with analyse_project()."
                if removed else
                "Nothing to remove — already clean."
            ),
        }

    # ------------------------------------------------------------------
    # Neighborhood exploration
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_neighborhood(symbol: str, project: str | None = None):
        """
        One-shot structural context for a symbol: callers (upstream), callees
        (downstream), sibling methods in the same class, and override /
        implements relationships.

        This is the tool to call when you want to understand a method's
        immediate surroundings in the call graph without traversing the
        full impact tree.

        Parameters:
          symbol  – Method name, signature fragment, or fully-qualified name.
          project – Optional project_id to scope the symbol lookup.
        """
        from codespine.analysis.impact import _resolve_method_metadata

        # Normalize FQN inputs: "Class#method(sig)" → "method(sig)"
        normalized = _normalize_symbol_input(symbol)

        project_clause = "AND f.project_id = $proj" if project else ""
        params: dict = {"q": normalized, "raw": symbol}
        if project:
            params["proj"] = project

        # 1. Resolve the symbol to method IDs.  Try both the normalized
        #    form and the raw input so exact-ID matches still work.
        method_recs = store.query_records(
            f"""
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
              AND (m.id = $q OR m.id = $raw
                   OR lower(m.name) = lower($q)
                   OR lower(m.signature) CONTAINS lower($q))
            RETURN m.id as id, m.name as name, m.signature as signature,
                   c.id as class_id, c.fqcn as class_fqcn,
                   f.path as file_path, f.project_id as project_id
            LIMIT 5
            """,
            params,
        )
        if not method_recs:
            return {"available": False, "note": f"Symbol '{symbol}' not found. Try find_symbol or search_hybrid."}

        target = method_recs[0]
        mid = target["id"]
        cid = target["class_id"]

        # 2. Callers (upstream) — exclude low-confidence cross-module fallback edges
        callers = store.query_records(
            """
            MATCH (caller:Method)-[r:CALLS]->(m:Method {id: $mid})
            WHERE coalesce(r.confidence, 0.5) >= 0.5
            RETURN caller.id as id, coalesce(r.confidence, 0.5) as confidence,
                   coalesce(r.reason, 'unknown') as reason
            """,
            {"mid": mid},
        )

        # 3. Callees (downstream) — exclude low-confidence cross-module fallback edges
        callees = store.query_records(
            """
            MATCH (m:Method {id: $mid})-[r:CALLS]->(callee:Method)
            WHERE coalesce(r.confidence, 0.5) >= 0.5
            RETURN callee.id as id, coalesce(r.confidence, 0.5) as confidence,
                   coalesce(r.reason, 'unknown') as reason
            """,
            {"mid": mid},
        )

        # 4. Siblings (same class, excluding self)
        siblings = store.query_records(
            """
            MATCH (m:Method)
            WHERE m.class_id = $cid AND m.id <> $mid
            RETURN m.id as id, m.name as name, m.signature as signature
            """,
            {"cid": cid, "mid": mid},
        )

        # 5. Override / implements relationships
        overrides_up = store.query_records(
            "MATCH (m:Method {id: $mid})-[:OVERRIDES]->(parent:Method) RETURN parent.id as id",
            {"mid": mid},
        )
        overrides_down = store.query_records(
            "MATCH (child:Method)-[:OVERRIDES]->(m:Method {id: $mid}) RETURN child.id as id",
            {"mid": mid},
        )

        # Bulk-resolve all referenced method IDs for human-readable output
        all_ids = (
            [c["id"] for c in callers]
            + [c["id"] for c in callees]
            + [o["id"] for o in overrides_up]
            + [o["id"] for o in overrides_down]
        )
        meta = _resolve_method_metadata(store, all_ids) if all_ids else {}

        def _enrich(items, extra_keys=None):
            enriched = []
            for item in items:
                m = meta.get(item["id"], {})
                entry = {
                    "id": item["id"],
                    "name": m.get("name") or item.get("name"),
                    "fqname": m.get("fqname") or item.get("signature"),
                    "class_fqcn": m.get("class_fqcn"),
                    "file_path": m.get("file_path"),
                    "project_id": m.get("project_id"),
                }
                if extra_keys:
                    for k in extra_keys:
                        if k in item:
                            entry[k] = item[k]
                enriched.append(entry)
            return enriched

        target_pid = target["project_id"]
        enriched_callers = _enrich(callers, extra_keys=["confidence", "reason"])
        # FR-11: label cross-project callers so consumers can separate them.
        local_callers = [c for c in enriched_callers if c.get("project_id") == target_pid]
        cross_project_callers = [c for c in enriched_callers if c.get("project_id") != target_pid]

        result = {
            "available": True,
            "target": {
                "id": mid,
                "name": target["name"],
                "signature": target["signature"],
                "class_fqcn": target["class_fqcn"],
                "file_path": target["file_path"],
                "project_id": target_pid,
            },
            "callers": local_callers,
            "cross_project_callers": cross_project_callers,
            "callees": _enrich(callees, extra_keys=["confidence", "reason"]),
            "siblings": [
                {"name": s["name"], "signature": s["signature"]}
                for s in siblings
            ],
            "overrides": _enrich(overrides_up),
            "overridden_by": _enrich(overrides_down),
            "summary": {
                "callers": len(local_callers),
                "cross_project_callers": len(cross_project_callers),
                "callees": len(callees),
                "siblings": len(siblings),
                "overrides": len(overrides_up),
                "overridden_by": len(overrides_down),
            },
        }
        return _staleness_meta(store, result)

    # ------------------------------------------------------------------
    # Single-file re-index
    # ------------------------------------------------------------------

    @mcp.tool()
    def reindex_file(file_path: str, project: str | None = None):
        """
        Incrementally re-index a single Java file (<1 s for typical files).

        Use this after editing a file to immediately refresh the graph without
        waiting for watch mode or running a full analysis.

        The file is parsed and its symbols are stored in the overlay (just like
        watch mode), so the updated data is immediately visible in search and
        find_symbol results.

        Parameters:
          file_path – Absolute path to the .java file.
          project   – Optional project_id. If omitted, the tool infers the
                      project by matching the file path against indexed projects.
        """
        import os as _os

        abs_fp = _os.path.abspath(file_path)
        if not _os.path.isfile(abs_fp) or not abs_fp.endswith(".java"):
            return {"available": False, "note": f"Not a valid .java file: {abs_fp}"}

        # Resolve project from indexed projects if not given
        if not project:
            try:
                projects = store.query_records(
                    "MATCH (p:Project) RETURN p.id as id, p.path as path"
                )
            except Exception as exc:
                return {"available": False, "note": f"DB read failed: {exc}"}
            for p in projects:
                if abs_fp.startswith(p["path"] + _os.sep):
                    project = p["id"]
                    break
            if not project:
                return {
                    "available": False,
                    "note": (
                        "Cannot determine project for this file. "
                        "Pass project=<project_id> explicitly."
                    ),
                }

        # Find the project path to use as root for indexing
        try:
            proj_recs = store.query_records(
                "MATCH (p:Project) WHERE p.id = $pid RETURN p.path as path LIMIT 1",
                {"pid": project},
            )
        except Exception as exc:
            return {"available": False, "note": f"DB read failed: {exc}"}
        if not proj_recs:
            return {"available": False, "note": f"Project '{project}' not found in index."}

        proj_path = proj_recs[0]["path"]

        # Use overlay-based single-file update (same mechanism as watch mode).
        # This avoids spawning a subprocess and contending with the write DB.
        from codespine.watch.watcher import _update_overlay_for_files

        t0 = time.time()
        try:
            result = _update_overlay_for_files(store, proj_path, project, [abs_fp])
            elapsed = round(time.time() - t0, 2)
        except Exception as exc:
            elapsed = round(time.time() - t0, 2)
            _LOGGER.warning("reindex_file failed: %s", exc)
            # Fall back to subprocess approach
            cmd = [
                sys.executable, "-m", "codespine.cli",
                "analyse", proj_path,
                "--incremental", "--no-embed", "--allow-running",
            ]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                stdout, stderr = proc.communicate(timeout=60)
                elapsed = round(time.time() - t0, 2)
                if proc.returncode != 0:
                    return {
                        "available": False,
                        "note": f"Re-index failed (code {proc.returncode})",
                        "error": (stderr or stdout or "").strip()[:500],
                    }
                return {
                    "available": True,
                    "file": abs_fp,
                    "project": project,
                    "elapsed_s": elapsed,
                    "note": f"Overlay update failed; fell back to full incremental re-index in {elapsed}s.",
                }
            except Exception as fallback_exc:
                return {"available": False, "note": f"Re-index error: overlay={exc}, subprocess={fallback_exc}"}

        return {
            "available": True,
            "file": abs_fp,
            "project": project,
            "elapsed_s": elapsed,
            "changed": result.get("changed", 0),
            "note": f"Re-indexed {abs_fp} via overlay in {elapsed}s.",
        }

    # ------------------------------------------------------------------
    # LLM-native interface tools (Phase 3-5)
    # ------------------------------------------------------------------

    @mcp.tool()
    def ask(question: str, project: str | None = None):
        """
        Natural language dispatcher. Ask anything about the codebase in plain English.

        Interprets the question and routes to the most appropriate underlying tool.
        Returns the tool's result plus dispatched_to and interpreted_as fields.

        Examples:
          "who calls PaymentService?"              → get_impact
          "what breaks if I change UserRepository?" → what_breaks
          "explain AuthController"                 → explain
          "find methods named processOrder"        → find_symbol
          "what injects OrderService?"             → find_injections
          "dead code in billing module"            → detect_dead_code
        """
        q = question.lower().strip()
        dispatched_to = "search_hybrid"
        interpreted_as = f"semantic search for: {question}"

        # Route by keyword patterns.
        _IMPACT_PATTERNS = ("who calls", "callers of", "what calls", "depends on")
        _BREAKS_PATTERNS = ("what breaks", "impact of", "what would break", "blast radius", "what changes if")
        _EXPLAIN_PATTERNS = ("what does", "what is", "explain", "describe", "tell me about")
        _INJECT_PATTERNS = ("what injects", "who injects", "di for", "injection", "autowired", "@inject")
        _DEAD_PATTERNS = ("dead code", "unused", "unreachable", "not called")
        _TEST_PATTERNS = ("test for", "tests for", "coverage for", "what tests")
        _FIND_PATTERNS = ("find method", "find class", "find field", "where is", "locate")

        def _extract_symbol(text: str, *prefixes: str) -> str:
            for prefix in prefixes:
                idx = text.find(prefix)
                if idx >= 0:
                    return question[idx + len(prefix):].strip().strip("'\"?")
            return question

        try:
            if any(p in q for p in _BREAKS_PATTERNS):
                sym = _extract_symbol(q, *_BREAKS_PATTERNS)
                dispatched_to = "what_breaks"
                interpreted_as = f"blast radius of changing: {sym}"
                return what_breaks(sym, project=project)
            elif any(p in q for p in _INJECT_PATTERNS):
                sym = _extract_symbol(q, *_INJECT_PATTERNS)
                dispatched_to = "find_injections"
                interpreted_as = f"DI consumers of: {sym}"
                return find_injections(sym, project=project)
            elif any(p in q for p in _DEAD_PATTERNS):
                dispatched_to = "detect_dead_code"
                interpreted_as = "dead code detection"
                return detect_dead_code(project=project)
            elif any(p in q for p in _EXPLAIN_PATTERNS):
                sym = _extract_symbol(q, *_EXPLAIN_PATTERNS)
                dispatched_to = "explain"
                interpreted_as = f"explain symbol: {sym}"
                return explain(sym, project=project)
            elif any(p in q for p in _IMPACT_PATTERNS):
                sym = _extract_symbol(q, *_IMPACT_PATTERNS)
                dispatched_to = "get_impact"
                interpreted_as = f"callers of: {sym}"
                return get_impact(sym, project=project)
            elif any(p in q for p in _TEST_PATTERNS):
                sym = _extract_symbol(q, *_TEST_PATTERNS)
                dispatched_to = "test_coverage"
                interpreted_as = f"test coverage for: {sym}"
                return test_coverage(sym, project=project)
            elif any(p in q for p in _FIND_PATTERNS):
                sym = _extract_symbol(q, *_FIND_PATTERNS)
                dispatched_to = "find_symbol"
                interpreted_as = f"find symbol: {sym}"
                return find_symbol(sym, project=project)
            else:
                dispatched_to = "search_hybrid"
                interpreted_as = f"semantic search for: {question}"
                result = search_hybrid(question, project=project)
                # Attach routing info to the result string.
                import json as _j
                try:
                    data = _j.loads(result)
                    data["dispatched_to"] = dispatched_to
                    data["interpreted_as"] = interpreted_as
                    return _json(data)
                except Exception:
                    return result
        except Exception as exc:
            return _safe_tool_response("ask", exc)

    @mcp.tool()
    def what_breaks(symbol: str, project: str | None = None):
        """
        What would break if this symbol changed?

        Intent-named wrapper around get_impact that also includes DI consumers.
        Returns a flat summary with risk_level to help prioritise refactoring.

        risk_level: "low" (<5 callers), "medium" (5–20), "high" (>20).
        """
        try:
            normalized = _normalize_symbol_input(symbol)
            result = analyze_impact(store, normalized, max_depth=4, project=project)
            if not result.get("resolved_to"):
                result = analyze_impact(store, symbol, max_depth=4, project=project)
            if not result.get("resolved_to"):
                return {"available": False, "note": f"Symbol '{symbol}' not found."}
            callers = result.get("impacted_callers", {})
            total = sum(len(v) for v in callers.values())
            risk = "low" if total < 5 else ("medium" if total < 20 else "high")
            return _staleness_meta(store, {
                "available": True,
                "symbol": symbol,
                "resolved_to": result.get("resolved_to", []),
                "risk_level": risk,
                "total_callers": total,
                "impacted_callers": callers,
                "self_callers": result.get("self_callers", []),
                "summary": result.get("summary", {}),
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("what_breaks", exc)

    @mcp.tool()
    def explain(symbol: str, project: str | None = None):
        """
        Compound understanding tool — chains symbol lookup + neighborhood + community.

        Returns a structured summary without needing multiple tool calls:
          matched    — what the symbol is (type, fqname, file_path, line)
          neighbors  — direct callers and callees
          community  — architectural cluster membership
        """
        try:
            normalized = _normalize_symbol_input(symbol)
            # 1. Find the symbol.
            sym_recs = store.query_records(
                """
                MATCH (s:Symbol)
                WHERE lower(s.name) = $namel OR lower(s.fqname) CONTAINS $namel
                RETURN s.id as id, s.kind as kind, s.name as name,
                       s.fqname as fqname, s.file_id as file_id, s.line as line
                LIMIT 5
                """,
                {"namel": normalized.lower()},
            )
            if not sym_recs:
                return {"available": False, "note": f"Symbol '{symbol}' not found."}

            # 2. Community membership for top match.
            top = sym_recs[0]
            community_info = store.query_records(
                """
                MATCH (s:Symbol {id: $sid})-[:IN_COMMUNITY]->(c:Community)
                RETURN c.id as id, c.label as label, c.cohesion as cohesion
                LIMIT 1
                """,
                {"sid": top["id"]},
            )

            # 3. Direct callers/callees (first-degree neighborhood via Method).
            callers = store.query_records(
                """
                MATCH (caller:Method)-[:CALLS]->(m:Method), (s:Symbol)
                WHERE s.id = $sid AND s.fqname CONTAINS m.signature
                RETURN caller.name as name, caller.id as id
                LIMIT 10
                """,
                {"sid": top["id"]},
            )
            callees = store.query_records(
                """
                MATCH (m:Method)-[:CALLS]->(callee:Method), (s:Symbol)
                WHERE s.id = $sid AND s.fqname CONTAINS m.signature
                RETURN callee.name as name, callee.id as id
                LIMIT 10
                """,
                {"sid": top["id"]},
            )

            return _staleness_meta(store, {
                "available": True,
                "symbol": symbol,
                "matched": sym_recs,
                "community": community_info[0] if community_info else None,
                "callers": callers,
                "callees": callees,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("explain", exc)

    @mcp.tool()
    def read_symbols(file_path: str, symbols: list[str] | None = None):
        """
        Read source code for specific symbols from a file — 60-70% fewer tokens than
        reading the whole file.

        Parameters:
          file_path – Absolute or relative path to the Java file.
          symbols   – List of method/class names to extract. If None, returns all.

        Returns each matched symbol's source lines as a separate entry.
        """
        try:
            abs_fp = os.path.abspath(file_path)
            if not os.path.isfile(abs_fp):
                return {"available": False, "note": f"File not found: {abs_fp}"}
            with open(abs_fp, "rb") as fh:
                source = fh.read()
            from codespine.indexer.java_parser import parse_java_source
            parsed = parse_java_source(source)
            lines = source.decode("utf-8", errors="replace").splitlines()
            results: list[dict] = []
            symbols_lower = {s.lower() for s in symbols} if symbols else None
            for cls in parsed.classes:
                if symbols_lower is None or cls.name.lower() in symbols_lower:
                    results.append({
                        "kind": "class",
                        "name": cls.name,
                        "fqcn": cls.fqcn,
                        "line_start": cls.line,
                    })
                for method in cls.methods:
                    if symbols_lower is None or method.name.lower() in symbols_lower:
                        # Extract source lines for just this method.
                        start = method.line - 1
                        end = min(len(lines), start + 60)  # cap at 60 lines per method
                        results.append({
                            "kind": "method",
                            "name": method.name,
                            "class": cls.fqcn,
                            "signature": method.signature,
                            "line_start": method.line,
                            "source": "\n".join(lines[start:end]),
                        })
            return {"available": True, "file": abs_fp, "symbols": results}
        except Exception as exc:
            return _safe_tool_response("read_symbols", exc)

    @mcp.tool()
    def semantic_summary(symbol: str, project: str | None = None):
        """
        Condensed view of a class or method — signatures and metadata only, no body.
        ~80 tokens vs ~800 for reading the full source. Ideal for understanding
        an API surface before making changes.
        """
        try:
            name_lower = symbol.lower()
            # Try class first.
            cls_recs = store.query_records(
                """
                MATCH (c:Class), (f:File)
                WHERE c.file_id = f.id AND (lower(c.name) = $namel OR lower(c.fqcn) CONTAINS $namel)
                RETURN c.id as id, c.name as name, c.fqcn as fqcn, c.package as package,
                       f.project_id as project_id, f.path as file_path
                LIMIT 3
                """,
                {"namel": name_lower},
            )
            if cls_recs:
                cls = cls_recs[0]
                methods = store.query_records(
                    """
                    MATCH (m:Method) WHERE m.class_id = $cid
                    RETURN m.name as name, m.signature as signature,
                           m.return_type as return_type, m.modifiers as modifiers,
                           m.is_constructor as is_constructor
                    """,
                    {"cid": cls["id"]},
                )
                public_methods = [m for m in methods if "public" in (m.get("modifiers") or [])]
                return _staleness_meta(store, {
                    "available": True,
                    "kind": "class",
                    "name": cls["name"],
                    "fqcn": cls["fqcn"],
                    "package": cls["package"],
                    "project_id": cls["project_id"],
                    "file_path": cls["file_path"],
                    "method_count": len(methods),
                    "public_method_count": len(public_methods),
                    "public_methods": [
                        {"name": m["name"], "signature": m["signature"], "return_type": m["return_type"]}
                        for m in public_methods[:20]
                    ],
                }, project, overlay_store=overlay_store)
            # Fall back to method.
            method_recs = store.query_records(
                """
                MATCH (m:Method), (c:Class)
                WHERE m.class_id = c.id AND lower(m.name) = $namel
                RETURN m.name as name, m.signature as signature, m.return_type as return_type,
                       m.modifiers as modifiers, c.fqcn as class_fqcn
                LIMIT 5
                """,
                {"namel": name_lower},
            )
            if method_recs:
                return _staleness_meta(store, {
                    "available": True,
                    "kind": "method",
                    "matches": method_recs,
                }, project, overlay_store=overlay_store)
            return {"available": False, "note": f"Symbol '{symbol}' not found."}
        except Exception as exc:
            return _safe_tool_response("semantic_summary", exc)

    @mcp.tool()
    def get_api_surface(class_name: str, project: str | None = None):
        """
        Return only the public interface of a class — public methods and fields.
        Excludes private/protected members. Ideal for understanding how to use
        a class without reading its full implementation.
        """
        try:
            name_lower = class_name.lower()
            proj_clause = "AND f.project_id = $proj" if project else ""
            params: dict = {"namel": name_lower}
            if project:
                params["proj"] = project
            cls_recs = store.query_records(
                f"""
                MATCH (c:Class), (f:File)
                WHERE c.file_id = f.id AND (lower(c.name) = $namel OR lower(c.fqcn) CONTAINS $namel)
                {proj_clause}
                RETURN c.id as id, c.name as name, c.fqcn as fqcn
                LIMIT 3
                """,
                params,
            )
            if not cls_recs:
                return {"available": False, "note": f"Class '{class_name}' not found."}
            cls = cls_recs[0]
            methods = store.query_records(
                """
                MATCH (m:Method) WHERE m.class_id = $cid AND 'public' IN m.modifiers
                RETURN m.name as name, m.signature as signature, m.return_type as return_type,
                       m.is_constructor as is_constructor
                """,
                {"cid": cls["id"]},
            )
            fields = store.query_records(
                """
                MATCH (s:Symbol), (c:Class)
                WHERE s.file_id = c.file_id AND c.id = $cid AND s.kind = 'field'
                  AND lower(s.fqname) STARTS WITH lower($fqcn)
                RETURN s.name as name, s.fqname as fqname, s.line as line
                LIMIT 20
                """,
                {"cid": cls["id"], "fqcn": cls["fqcn"]},
            )
            return _staleness_meta(store, {
                "available": True,
                "class": cls["fqcn"],
                "public_methods": methods,
                "public_fields": fields,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("get_api_surface", exc)

    @mcp.tool()
    def file_context(file_path: str, project: str | None = None):
        """
        Auto-inject graph context for a file before editing it.

        Returns:
          symbols      — all classes and methods declared in the file
          callers      — external methods that call into this file
          community    — architectural cluster this file belongs to
          coupling     — files that frequently change alongside this one
          overlay_dirty — whether this file has uncommitted overlay changes
        """
        try:
            abs_fp = os.path.abspath(file_path)
            # Resolve file_id.
            file_recs = store.query_records(
                "MATCH (f:File) WHERE f.path = $path RETURN f.id as id, f.project_id as pid LIMIT 1",
                {"path": abs_fp},
            )
            if not file_recs:
                return {"available": False, "note": f"File '{abs_fp}' not indexed. Run analyse_project() first."}
            f_id = file_recs[0]["id"]
            pid = file_recs[0]["pid"]
            if project is None:
                project = pid

            symbols = store.query_records(
                "MATCH (s:Symbol) WHERE s.file_id = $fid RETURN s.kind as kind, s.name as name, s.line as line",
                {"fid": f_id},
            )
            callers = store.query_records(
                """
                MATCH (caller:Method)-[:CALLS]->(m:Method), (cm:Class), (cf:File)
                WHERE m.class_id = cm.id AND cm.file_id = $fid
                  AND caller.class_id <> cm.id
                WITH DISTINCT caller
                MATCH (cc:Class) WHERE caller.class_id = cc.id
                RETURN caller.name as name, cc.fqcn as class_fqcn
                LIMIT 20
                """,
                {"fid": f_id},
            )
            community = store.query_records(
                """
                MATCH (s:Symbol)-[:IN_COMMUNITY]->(c:Community)
                WHERE s.file_id = $fid
                RETURN c.label as label, count(s) as symbol_count
                LIMIT 3
                """,
                {"fid": f_id},
            )
            coupling = store.query_records(
                """
                MATCH (f:File {id: $fid})-[r:CO_CHANGED_WITH]->(g:File)
                RETURN g.path as coupled_file, r.strength as strength, r.cochanges as cochanges
                ORDER BY r.strength DESC LIMIT 10
                """,
                {"fid": f_id},
            )
            # Check overlay dirty state.
            overlay_dirty = False
            if overlay_store is not None:
                try:
                    from codespine.overlay.merge import _load_overlay_docs, suppressed_file_ids
                    docs = _load_overlay_docs(overlay_store, project)
                    overlay_dirty = f_id in suppressed_file_ids(docs)
                except Exception:
                    pass

            return _staleness_meta(store, {
                "available": True,
                "file": abs_fp,
                "project": project,
                "symbols": symbols,
                "external_callers": callers,
                "community": community,
                "coupling": coupling,
                "overlay_dirty": overlay_dirty,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("file_context", exc)

    @mcp.tool()
    def pre_flight_check(
        file_path: str,
        symbols: list[str] | None = None,
        change_type: str = "modify",
        project: str | None = None,
    ):
        """
        Blast radius analysis before making changes — call this BEFORE editing.

        Parameters:
          file_path   – The file you plan to modify.
          symbols     – Specific method/class names you plan to change. If None,
                        analyses all symbols in the file.
          change_type – "modify" | "delete" | "rename" (informational only).

        Returns:
          affected_methods  — total methods impacted across all depths
          affected_projects — unique projects containing affected code
          risk_level        — "low" | "medium" | "high"
          per_symbol        — per-symbol blast radius breakdown
        """
        try:
            abs_fp = os.path.abspath(file_path)
            file_recs = store.query_records(
                "MATCH (f:File) WHERE f.path = $path RETURN f.id as id, f.project_id as pid LIMIT 1",
                {"path": abs_fp},
            )
            if not file_recs:
                return {"available": False, "note": f"File '{abs_fp}' not indexed."}
            f_id = file_recs[0]["id"]
            if project is None:
                project = file_recs[0]["pid"]

            # Find symbols to analyse.
            if symbols:
                sym_query_list = symbols
            else:
                method_recs = store.query_records(
                    """
                    MATCH (m:Method), (c:Class)
                    WHERE m.class_id = c.id AND c.file_id = $fid
                    RETURN m.name as name
                    LIMIT 20
                    """,
                    {"fid": f_id},
                )
                sym_query_list = [r["name"] for r in method_recs]

            per_symbol: list[dict] = []
            total_affected: set[str] = set()
            total_projects: set[str] = set()
            for sym in sym_query_list[:10]:  # cap at 10 symbols to prevent timeout
                result = analyze_impact(store, sym, max_depth=3, project=project)
                callers = result.get("impacted_callers", {})
                all_c = [item for items in callers.values() for item in items]
                for item in all_c:
                    total_affected.add(item.get("symbol", ""))
                    if item.get("project_id"):
                        total_projects.add(item["project_id"])
                per_symbol.append({
                    "symbol": sym,
                    "direct_callers": len(callers.get("1", [])),
                    "total_callers": len(all_c),
                })

            total = len(total_affected)
            risk = "low" if total < 5 else ("medium" if total < 20 else "high")
            return _staleness_meta(store, {
                "available": True,
                "file": abs_fp,
                "change_type": change_type,
                "risk_level": risk,
                "affected_methods": total,
                "affected_projects": list(total_projects),
                "per_symbol": per_symbol,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("pre_flight_check", exc)

    @mcp.tool()
    def related(symbol: str, limit: int = 5, project: str | None = None):
        """
        Find symbols tightly coupled to a given symbol.

        Combines multiple coupling signals:
          - co-change coupling (git history)
          - shared community membership
          - direct call relationship
          - same-class siblings

        Returns ranked list of related symbols with coupling reason.
        """
        try:
            normalized = _normalize_symbol_input(symbol)
            sym_recs = store.query_records(
                """
                MATCH (s:Symbol)
                WHERE lower(s.name) = $namel OR lower(s.fqname) CONTAINS $namel
                RETURN s.id as id, s.name as name, s.fqname as fqname, s.file_id as file_id
                LIMIT 3
                """,
                {"namel": normalized.lower()},
            )
            if not sym_recs:
                return {"available": False, "note": f"Symbol '{symbol}' not found."}

            top = sym_recs[0]
            seen: dict[str, dict] = {}

            # Co-change coupling — files that changed together.
            try:
                coupling = store.query_records(
                    """
                    MATCH (f:File {id: $fid})-[r:CO_CHANGED_WITH]->(g:File)
                    MATCH (s:Symbol) WHERE s.file_id = g.id
                    RETURN s.id as id, s.name as name, r.strength as score, 'co_change' as reason
                    ORDER BY r.strength DESC LIMIT $lim
                    """,
                    {"fid": top["file_id"], "lim": limit * 2},
                )
                for r in coupling:
                    if r["id"] != top["id"]:
                        seen.setdefault(r["id"], {"symbol": r["id"], "name": r["name"], "score": 0.0, "reasons": []})
                        seen[r["id"]]["score"] = max(seen[r["id"]]["score"], float(r.get("score") or 0))
                        seen[r["id"]]["reasons"].append("co_change")
            except Exception:
                pass

            # Same community.
            try:
                community = store.query_records(
                    """
                    MATCH (s:Symbol {id: $sid})-[:IN_COMMUNITY]->(c:Community)<-[:IN_COMMUNITY]-(t:Symbol)
                    WHERE t.id <> $sid
                    RETURN t.id as id, t.name as name, 0.6 as score, 'community' as reason
                    LIMIT $lim
                    """,
                    {"sid": top["id"], "lim": limit * 2},
                )
                for r in community:
                    seen.setdefault(r["id"], {"symbol": r["id"], "name": r["name"], "score": 0.0, "reasons": []})
                    seen[r["id"]]["score"] = max(seen[r["id"]]["score"], 0.6)
                    if "community" not in seen[r["id"]]["reasons"]:
                        seen[r["id"]]["reasons"].append("community")
            except Exception:
                pass

            ranked = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:limit]
            return _staleness_meta(store, {
                "available": True,
                "symbol": symbol,
                "related": ranked,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("related", exc)

    @mcp.tool()
    def test_coverage(symbol: str, project: str | None = None):
        """
        Find tests that cover a given symbol (directly or transitively, depth ≤ 2).

        Returns:
          covered_by_tests — test methods that call the symbol (direct or indirect)
          coverage_status  — "direct" | "indirect" | "uncovered"
        """
        try:
            normalized = _normalize_symbol_input(symbol)
            sym_recs = store.query_records(
                """
                MATCH (s:Symbol)
                WHERE lower(s.name) = $namel OR lower(s.fqname) CONTAINS $namel
                RETURN s.id as id, s.name as name, s.fqname as fqname
                LIMIT 5
                """,
                {"namel": normalized.lower()},
            )
            if not sym_recs:
                return {"available": False, "note": f"Symbol '{symbol}' not found."}

            top_ids = [r["id"] for r in sym_recs]
            # Resolve symbol IDs to method IDs.
            method_recs = store.query_records(
                """
                MATCH (m:Method), (s:Symbol)
                WHERE s.id IN $sids AND s.fqname CONTAINS m.signature
                RETURN m.id as mid, m.name as mname
                """,
                {"sids": top_ids},
            )
            if not method_recs:
                return {"available": False, "note": f"No method found for '{symbol}'."}

            mid = method_recs[0]["mid"]
            # Direct test callers.
            direct = store.query_records(
                """
                MATCH (t:Method)-[:CALLS]->(m:Method {id: $mid})
                WHERE t.is_test = true
                MATCH (tc:Class) WHERE t.class_id = tc.id
                RETURN t.id as id, t.name as name, tc.fqcn as test_class, 1 as depth
                LIMIT 20
                """,
                {"mid": mid},
            )
            # Indirect (depth 2).
            indirect = store.query_records(
                """
                MATCH (t:Method)-[:CALLS]->(x:Method)-[:CALLS]->(m:Method {id: $mid})
                WHERE t.is_test = true
                MATCH (tc:Class) WHERE t.class_id = tc.id
                RETURN t.id as id, t.name as name, tc.fqcn as test_class, 2 as depth
                LIMIT 20
                """,
                {"mid": mid},
            )
            covered_by = direct + [r for r in indirect if r["id"] not in {d["id"] for d in direct}]
            status = "uncovered" if not covered_by else ("direct" if direct else "indirect")
            return _staleness_meta(store, {
                "available": True,
                "symbol": symbol,
                "coverage_status": status,
                "covered_by_tests": covered_by,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("test_coverage", exc)

    @mcp.tool()
    def diff_impact(git_ref: str = "HEAD~1", project: str | None = None):
        """
        Graph-level impact analysis for uncommitted changes or a git ref.

        Finds Java files changed since git_ref, then runs blast-radius analysis
        on each changed symbol to produce a PR-level summary.

        Parameters:
          git_ref – Git reference to diff against (default: HEAD~1 = last commit).
                    Also accepts branch names, commit SHAs, or "HEAD" (working tree diff).
        """
        try:
            repo = _resolve_repo_path(store, project, repo_path_provider)
            if not _git_available(repo):
                return {"available": False, "note": "Not a git repository."}

            # Find changed Java files.
            diff_cmd = ["git", "diff", "--name-only", git_ref]
            r = subprocess.run(diff_cmd, cwd=repo, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return {"available": False, "note": f"git diff failed: {r.stderr.strip()[:200]}"}
            changed_files = [
                os.path.join(repo, line.strip())
                for line in r.stdout.splitlines()
                if line.strip().endswith(".java")
            ]
            if not changed_files:
                return {"available": True, "changed_java_files": 0, "note": "No Java files changed."}

            # Find symbols in changed files.
            abs_paths = [os.path.abspath(f) for f in changed_files]
            file_recs = store.query_records(
                "MATCH (f:File) WHERE f.path IN $paths RETURN f.id as fid, f.path as path, f.project_id as pid",
                {"paths": abs_paths},
            )
            if not file_recs:
                return {"available": True, "note": "Changed files are not indexed. Run analyse_project() first."}

            all_affected: set[str] = set()
            all_projects: set[str] = set()
            per_file: list[dict] = []
            for fr in file_recs[:10]:  # cap to avoid timeout
                method_recs = store.query_records(
                    """
                    MATCH (m:Method), (c:Class) WHERE m.class_id = c.id AND c.file_id = $fid
                    RETURN m.name as name LIMIT 10
                    """,
                    {"fid": fr["fid"]},
                )
                file_callers: set[str] = set()
                for mr in method_recs[:5]:
                    impact = analyze_impact(store, mr["name"], max_depth=2, project=fr["pid"])
                    callers = impact.get("impacted_callers", {})
                    for items in callers.values():
                        for item in items:
                            file_callers.add(item.get("symbol", ""))
                            if item.get("project_id"):
                                all_projects.add(item["project_id"])
                all_affected.update(file_callers)
                per_file.append({"file": fr["path"], "affected_callers": len(file_callers)})

            total = len(all_affected)
            risk = "low" if total < 5 else ("medium" if total < 20 else "high")
            return _staleness_meta(store, {
                "available": True,
                "git_ref": git_ref,
                "changed_java_files": len(changed_files),
                "risk_level": risk,
                "total_affected_methods": total,
                "affected_projects": list(all_projects),
                "per_file": per_file,
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("diff_impact", exc)

    @mcp.tool()
    def find_pattern(description: str, project: str | None = None):
        """
        Find structural code patterns by name or description.

        Recognises common Java design patterns by name and finds matching classes:
          "singleton", "factory", "builder", "observer", "repository",
          "service", "controller", "entity"

        Falls back to semantic search for other descriptions.
        """
        try:
            desc_lower = description.lower().strip()
            pattern_clauses: dict[str, str] = {
                "singleton": "lower(c.name) CONTAINS 'singleton' OR 'getInstance' IN [m.name | m IN methods]",
                "factory": "lower(c.name) ENDS WITH 'factory' OR lower(c.name) ENDS WITH 'factoryimpl'",
                "builder": "lower(c.name) ENDS WITH 'builder'",
                "observer": "lower(c.name) ENDS WITH 'observer' OR lower(c.name) ENDS WITH 'listener'",
                "repository": "lower(c.name) CONTAINS 'repository'",
                "service": "lower(c.name) ENDS WITH 'service' OR lower(c.name) ENDS WITH 'serviceimpl'",
                "controller": "lower(c.name) ENDS WITH 'controller'",
                "entity": "lower(c.name) ENDS WITH 'entity' OR lower(c.name) ENDS WITH 'model'",
            }
            matched_pattern: str | None = None
            for pattern_name in pattern_clauses:
                if pattern_name in desc_lower:
                    matched_pattern = pattern_name
                    break

            if matched_pattern:
                proj_clause = "AND f.project_id = $proj" if project else ""
                params: dict = {"namel": matched_pattern}
                if project:
                    params["proj"] = project
                classes = store.query_records(
                    f"""
                    MATCH (c:Class), (f:File)
                    WHERE c.file_id = f.id AND lower(c.name) CONTAINS $namel {proj_clause}
                    RETURN c.name as name, c.fqcn as fqcn, f.path as file_path,
                           f.project_id as project_id
                    LIMIT 20
                    """,
                    params,
                )
                return _staleness_meta(store, {
                    "available": True,
                    "pattern": matched_pattern,
                    "matches": classes,
                }, project, overlay_store=overlay_store)
            else:
                # Semantic fallback.
                results = search_hybrid(description, project=project)
                import json as _j
                try:
                    data = _j.loads(results)
                    data["pattern"] = None
                    data["note"] = f"No structural pattern matched '{description}'; showing semantic results."
                    return _json(data)
                except Exception:
                    return results
        except Exception as exc:
            return _safe_tool_response("find_pattern", exc)

    # ------------------------------------------------------------------
    # rename_plan  (FR-11 / Phase 5)
    # ------------------------------------------------------------------

    @mcp.tool()
    def rename_plan(symbol: str, new_name: str, project: str | None = None):
        """
        Safe cross-project rename plan for a method, class, or field.

        Finds all references to the symbol (callers, overrides, interface
        declarations, direct mentions) and returns a structured list of
        files_to_modify with the current text and suggested replacement.

        This tool does NOT modify files — it produces a plan that you (or your
        editor) can apply.  Review the plan before making changes.

        Parameters:
          symbol   – Method name, class name, or FQN to rename.
          new_name – The desired new name (simple name, not FQN).
          project  – Optional project scope for the initial symbol lookup.
        """
        try:
            normalized = _normalize_symbol_input(symbol)
            project_clause = "AND f.project_id = $proj" if project else ""
            params: dict = {"q": normalized, "raw": symbol}
            if project:
                params["proj"] = project

            # 1. Resolve to methods + classes with the given name.
            method_recs = store.query_records(
                f"""
                MATCH (m:Method), (c:Class), (f:File)
                WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
                  AND (m.id = $q OR m.id = $raw
                       OR lower(m.name) = lower($q)
                       OR lower(m.signature) CONTAINS lower($q))
                RETURN m.id as id, m.name as name, m.signature as signature,
                       c.fqcn as class_fqcn, f.path as file_path,
                       f.project_id as project_id, 'method' as kind
                LIMIT 20
                """,
                params,
            )
            class_recs = store.query_records(
                f"""
                MATCH (c:Class), (f:File)
                WHERE c.file_id = f.id {project_clause}
                  AND (c.id = $q OR c.id = $raw
                       OR lower(c.name) = lower($q) OR lower(c.fqcn) = lower($q))
                RETURN c.id as id, c.name as name, c.fqcn as class_fqcn,
                       f.path as file_path, f.project_id as project_id, 'class' as kind
                LIMIT 10
                """,
                params,
            )

            all_targets = method_recs + class_recs
            if not all_targets:
                return {
                    "available": False,
                    "note": f"Symbol '{symbol}' not found. Try find_symbol() first.",
                }

            # 2. Collect all files that declare or reference the targets.
            target_ids = [r["id"] for r in all_targets]
            declaration_files: dict[str, dict] = {}  # file_path → info

            # Declaration sites.
            for rec in all_targets:
                fp = rec.get("file_path", "")
                if fp:
                    declaration_files.setdefault(fp, {
                        "file_path": fp,
                        "project_id": rec.get("project_id"),
                        "changes": [],
                    })["changes"].append({
                        "kind": "declaration",
                        "symbol_kind": rec.get("kind", "method"),
                        "current_name": rec.get("name", symbol),
                        "suggested_name": new_name,
                        "note": f"Rename {rec.get('kind','method')} declaration",
                    })

            # Caller sites (only methods have call sites).
            method_ids = [r["id"] for r in method_recs]
            if method_ids:
                ph = ", ".join("$mid" + str(i) for i in range(len(method_ids)))
                caller_params = {f"mid{i}": v for i, v in enumerate(method_ids)}
                callers = store.query_records(
                    f"""
                    MATCH (caller:Method)-[:CALLS]->(m:Method), (c:Class), (f:File)
                    WHERE m.id IN [{ph}]
                      AND caller.class_id = c.id AND c.file_id = f.id
                    RETURN DISTINCT f.path as file_path, f.project_id as project_id,
                           caller.name as caller_name
                    LIMIT 100
                    """,
                    caller_params,
                )
                for cr in callers:
                    fp = cr.get("file_path", "")
                    if fp:
                        declaration_files.setdefault(fp, {
                            "file_path": fp,
                            "project_id": cr.get("project_id"),
                            "changes": [],
                        })["changes"].append({
                            "kind": "call_site",
                            "current_text": symbol,
                            "suggested_text": new_name,
                            "note": f"Call site in {cr.get('caller_name','<unknown>')}",
                        })

            # Override sites.
            for method_id in method_ids:
                overrides = store.query_records(
                    """
                    MATCH (child:Method)-[:OVERRIDES]->(m:Method {id: $mid}),
                          (cc:Class), (ff:File)
                    WHERE child.class_id = cc.id AND cc.file_id = ff.id
                    RETURN ff.path as file_path, ff.project_id as project_id,
                           child.name as name
                    """,
                    {"mid": method_id},
                )
                for ov in overrides:
                    fp = ov.get("file_path", "")
                    if fp:
                        declaration_files.setdefault(fp, {
                            "file_path": fp,
                            "project_id": ov.get("project_id"),
                            "changes": [],
                        })["changes"].append({
                            "kind": "override",
                            "current_name": ov.get("name", symbol),
                            "suggested_name": new_name,
                            "note": "Override — must match new name",
                        })

            files_to_modify = sorted(
                declaration_files.values(), key=lambda x: x.get("file_path", "")
            )
            projects_affected = {f["project_id"] for f in files_to_modify if f.get("project_id")}

            return _staleness_meta(store, {
                "available": True,
                "symbol": symbol,
                "new_name": new_name,
                "targets_found": len(all_targets),
                "files_to_modify": files_to_modify,
                "files_count": len(files_to_modify),
                "projects_affected": list(projects_affected),
                "note": (
                    f"Found {len(files_to_modify)} files to update. "
                    "This is a plan only — no files have been changed."
                ),
            }, project, overlay_store=overlay_store)
        except Exception as exc:
            return _safe_tool_response("rename_plan", exc)

    # ------------------------------------------------------------------
    # Advanced / raw access
    # ------------------------------------------------------------------

    @mcp.tool()
    def run_cypher(query: str):
        """Run a raw Cypher query against the graph. For advanced exploration."""
        records = store.query_records(query)
        return {"available": True, "records": records, "count": len(records)}

    return _raw_mcp
