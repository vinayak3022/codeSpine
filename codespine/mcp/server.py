from __future__ import annotations

import subprocess
import sys
import tempfile
import time

from fastmcp import FastMCP

from codespine import __version__
from codespine.analysis.community import detect_communities, symbol_community
from codespine.analysis.context import build_symbol_context
from codespine.analysis.coupling import get_coupling
from codespine.analysis.deadcode import detect_dead_code as detect_dead_code_analysis
from codespine.analysis.flow import trace_execution_flows as trace_flows_analysis
from codespine.analysis.impact import analyze_impact
from codespine.diff.branch_diff import compare_branches as compare_branches_analysis
from codespine.search.hybrid import hybrid_search


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
        recs = store.query_records(
            "MATCH (p:Project) WHERE p.id = $pid RETURN p.path as path LIMIT 1",
            {"pid": project},
        )
        if recs and recs[0].get("path"):
            return recs[0]["path"]
    return repo_path_provider()


def _no_symbols_response(note: str = "No symbols indexed. Run 'codespine analyse <path>' first.") -> dict:
    return {"available": False, "note": note}


def build_mcp_server(store, repo_path_provider):
    mcp = FastMCP("codespine")

    # Background job state (per-server-instance, persists across tool calls)
    _watch: dict = {"proc": None, "path": None, "started_at": None, "interval": 30}
    _analyse: dict = {"proc": None, "path": None, "started_at": None, "log_path": None, "returncode": None}

    # ------------------------------------------------------------------
    # Connectivity / feature discovery
    # ------------------------------------------------------------------

    @mcp.tool()
    def ping():
        """Verify the MCP server is alive. Call this first to confirm connectivity."""
        return {"status": "ok", "version": __version__}

    @mcp.tool()
    def get_capabilities():
        """
        Return what is indexed and which features are available RIGHT NOW.
        Call this before other tools so you know what's ready without trial-and-error.
        Features marked false may need 'codespine analyse --deep' or optional dependencies.
        """
        projects = store.query_records(
            "MATCH (p:Project) RETURN p.id as id, p.path as path, p.indexed_at as indexed_at"
        )
        sym_q = store.query_records("MATCH (s:Symbol) RETURN count(s) as count")
        comm_q = store.query_records("MATCH (c:Community) RETURN count(c) as count")
        flow_q = store.query_records("MATCH (f:Flow) RETURN count(f) as count")
        coup_q = store.query_records("MATCH ()-[r:CO_CHANGED_WITH]->() RETURN count(r) as count")

        from codespine.search.vector import _load_model
        has_embeddings = _load_model() is not None

        repo = repo_path_provider()
        git_ok = _git_available(repo)

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

        return {
            "available": True,
            "indexed_projects": projects,
            "symbol_count": n_sym,
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
                "watch_mode": True,
                "analyse_project": True,
            },
            "background_jobs": {
                "watch_running": watch_running,
                "watch_path": _watch["path"] if watch_running else None,
                "analyse_running": analyse_running,
                "analyse_path": _analyse["path"] if analyse_running else None,
            },
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Project listing
    # ------------------------------------------------------------------

    @mcp.tool()
    def list_projects():
        """List all indexed projects with their symbol and file counts."""
        projects = store.query_records(
            "MATCH (p:Project) RETURN p.id as id, p.path as path, p.indexed_at as indexed_at"
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
        return {"available": True, "results": results}

    @mcp.tool()
    def get_impact(symbol: str, max_depth: int = 4, project: str | None = None):
        """
        Caller-tree impact analysis for a symbol.
        project scopes the target symbol lookup; cross-project callers are always included.
        """
        result = analyze_impact(store, symbol, max_depth=max_depth, project=project)
        if not result.get("targets_resolved"):
            return {"available": False, "note": f"Symbol '{symbol}' not found in the index."}
        return {"available": True, **result}

    @mcp.tool()
    def detect_dead_code(limit: int = 200, project: str | None = None):
        """
        Detect methods with no incoming calls (after Java-aware exemptions).
        Pass project to scope to a single module.
        """
        dead = detect_dead_code_analysis(store, limit=limit, project=project)
        if dead is None:
            return _no_symbols_response()
        return {"available": True, "dead_code": dead, "count": len(dead)}

    @mcp.tool()
    def trace_execution_flows(entry_symbol: str | None = None, max_depth: int = 6, project: str | None = None):
        """
        Trace execution flows from entry points (main methods, tests).
        Pass project to scope entry-point discovery to a single module.
        """
        flows = trace_flows_analysis(store, entry_symbol=entry_symbol, max_depth=max_depth, project=project)
        if not flows:
            return _no_symbols_response("No entry points found. Run 'codespine analyse --deep' or provide entry_symbol.")
        return {"available": True, "flows": flows}

    @mcp.tool()
    def get_symbol_community(symbol: str):
        """Return the architectural community cluster a symbol belongs to."""
        detect_communities(store)
        result = symbol_community(store, symbol)
        if not result.get("matches"):
            return {"available": False, "note": "No community data yet. Run 'codespine analyse --deep'."}
        return {"available": True, **result}

    @mcp.tool()
    def get_change_coupling(
        symbol: str | None = None,
        months: int = 6,
        min_strength: float = 0.3,
        min_cochanges: int = 3,
    ):
        """
        Files that historically change together (git co-change coupling).
        Requires 'codespine analyse --deep' to have been run.
        """
        result = get_coupling(store, symbol=symbol, months=months, min_strength=min_strength, min_cochanges=min_cochanges)
        if not result:
            return {
                "available": False,
                "note": "No coupling data. Run 'codespine analyse --deep' with a git repository.",
            }
        return {"available": True, "coupling": result}

    @mcp.tool()
    def get_symbol_context(query: str, max_depth: int = 3, project: str | None = None):
        """
        One-shot deep context for a symbol: search + impact + community + flows.
        Pass project to scope the search to a single indexed module.
        """
        result = build_symbol_context(store, query, max_depth=max_depth, project=project)
        if not result.get("search_candidates"):
            return _no_symbols_response()
        return {"available": True, **result}

    @mcp.tool()
    def get_codebase_stats():
        """
        Per-project and aggregate stats: files, classes, methods, call edges, embeddings.

        Use this to understand the size and coverage of each indexed project before
        deciding which project= scope to pass to analysis tools.
        """
        projects = store.query_records(
            "MATCH (p:Project) RETURN p.id as id, p.path as path ORDER BY p.id"
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
            })
            total_files += n_files
            total_classes += n_classes
            total_methods += n_methods
            total_calls += n_calls
            total_emb += n_emb

        return {
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
        }

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
          kind    – Optional filter: "class" or "method".
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

        classes: list[dict] = []
        methods: list[dict] = []

        if kind != "method":
            c_recs = store.query_records(
                f"""
                MATCH (c:Class), (f:File)
                WHERE c.file_id = f.id {project_clause}
                  AND (lower(c.name) = $namel
                    OR lower(c.fqcn) = $namel
                    OR lower(c.fqcn) CONTAINS $namel
                    OR lower(c.name) CONTAINS $namel)
                RETURN c.id as id, c.name as name, c.fqcn as fqname,
                       c.package as package,
                       f.project_id as project_id, f.path as file_path
                LIMIT $lim
                """,
                params,
            )
            classes = c_recs

        if kind != "class":
            m_recs = store.query_records(
                f"""
                MATCH (m:Method), (c:Class), (f:File)
                WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
                  AND (lower(m.name) = $namel
                    OR lower(m.signature) CONTAINS $namel)
                RETURN m.id as id, m.name as name,
                       m.signature as fqname,
                       c.fqcn as class_fqcn,
                       f.project_id as project_id, f.path as file_path,
                       m.return_type as return_type
                LIMIT $lim
                """,
                params,
            )
            methods = m_recs

        total = len(classes) + len(methods)
        if total == 0:
            return {
                "available": False,
                "note": f"No symbols found matching '{name}'. Try a shorter prefix or use search_hybrid for fuzzy matching.",
            }

        # Group by project_id so agents can see the landscape at a glance
        by_project: dict[str, dict] = {}
        for c in classes:
            pid = c.get("project_id", "?")
            by_project.setdefault(pid, {"classes": [], "methods": []})
            by_project[pid]["classes"].append(c)
        for m in methods:
            pid = m.get("project_id", "?")
            by_project.setdefault(pid, {"classes": [], "methods": []})
            by_project[pid]["methods"].append(m)

        return {
            "available": True,
            "query": name,
            "total_matches": total,
            "by_project": by_project,
            "note": (
                f"Found {total} match(es). If multiple projects contain the same name, "
                "pass project=<project_id> to subsequent tools to avoid cross-project ambiguity."
            ) if total > 1 else None,
        }

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
            ORDER BY f.project_id, c.package
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

        return {
            "available": True,
            "total_packages": len(recs),
            "by_project": by_project,
        }

    # ------------------------------------------------------------------
    # Git tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def git_log(file_path: str | None = None, limit: int = 20, project: str | None = None):
        """
        Recent git commits for the project (or a specific file).
        Returns available=false if the directory is not a git repository.
        Use project=<project_id> to target a specific indexed module's repo.
        """
        repo = _resolve_repo_path(store, project, repo_path_provider)
        if not _git_available(repo):
            return {"available": False, "note": "Not a git repository (or git not installed)."}
        cmd = ["git", "log", f"--max-count={limit}", "--oneline", "--no-decorate"]
        if file_path:
            cmd += ["--", file_path]
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {"available": False, "error": r.stderr.strip()}
        return {
            "available": True,
            "project": project or repo,
            "log": r.stdout.strip().splitlines(),
        }

    @mcp.tool()
    def git_diff(ref: str = "HEAD", file_path: str | None = None, project: str | None = None):
        """
        Show git diff (working tree vs ref, or between two refs separated by '...').
        Output is truncated to 200 lines.
        Returns available=false if the directory is not a git repository.
        """
        repo = _resolve_repo_path(store, project, repo_path_provider)
        if not _git_available(repo):
            return {"available": False, "note": "Not a git repository (or git not installed)."}
        cmd = ["git", "diff", ref]
        if file_path:
            cmd += ["--", file_path]
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return {"available": False, "error": r.stderr.strip()}
        lines = r.stdout.splitlines()
        truncated = False
        if len(lines) > 200:
            lines = lines[:200]
            truncated = True
        return {
            "available": True,
            "project": project or repo,
            "diff": "\n".join(lines),
            "truncated": truncated,
        }

    @mcp.tool()
    def compare_branches(base_ref: str, head_ref: str):
        """Symbol-level diff between two git refs (branches, tags, commits)."""
        repo = repo_path_provider()
        if not _git_available(repo):
            return {"available": False, "note": "Not a git repository (or git not installed)."}
        result = compare_branches_analysis(repo, base_ref, head_ref)
        return {"available": True, **result}

    # ------------------------------------------------------------------
    # Watch mode tools
    # ------------------------------------------------------------------

    @mcp.tool()
    def start_watch(path: str, global_interval: int = 30):
        """
        Start watching a project directory for Java file changes and auto-reindex.

        Watch mode monitors .java files for changes and incrementally re-indexes
        only the modified module(s). It also periodically runs community detection,
        flow tracing, and coupling analysis every global_interval seconds.

        RECOMMENDATION: Enable watch mode during active development. It keeps
        CodeSpine's graph in sync with your code changes in real time, making
        subsequent searches, impact analysis, and dead-code detection always
        reflect the latest state of the codebase.

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

        import tempfile as _tempfile
        watch_err_file = _tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", prefix="codespine_watch_", delete=False
        )
        watch_err_path = watch_err_file.name
        watch_err_file.close()

        proc = subprocess.Popen(
            [
                sys.executable, "-m", "codespine.cli",
                "watch", "--path", abs_path,
                "--global-interval", str(global_interval),
            ],
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
            "note": (
                "Watch mode started. CodeSpine will auto-reindex on every .java file change. "
                f"Global analyses (communities, flows, coupling) refresh every {global_interval}s."
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
            return {
                "available": False,
                "note": f"Reset failed: {proc.stderr.strip() or proc.stdout.strip()}",
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
            return {
                "available": False,
                "note": f"Reset failed: {proc.stderr.strip() or proc.stdout.strip()}",
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

    # ------------------------------------------------------------------
    # Advanced / raw access
    # ------------------------------------------------------------------

    @mcp.tool()
    def run_cypher(query: str):
        """Run a raw Cypher query against the graph. For advanced exploration."""
        return store.query_records(query)

    return mcp
