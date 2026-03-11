from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time

import click
import psutil

from codespine.analysis.community import detect_communities, symbol_community
from codespine.analysis.context import build_symbol_context
from codespine.analysis.coupling import compute_coupling, get_coupling
from codespine.analysis.crossmodule import link_cross_module_calls
from codespine.analysis.deadcode import detect_dead_code
from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact
from codespine.config import SETTINGS
from codespine.db.store import GraphStore
from codespine.diff.branch_diff import compare_branches
from codespine.indexer.engine import JavaIndexer
from codespine.mcp.server import build_mcp_server
from codespine.search.hybrid import hybrid_search
from codespine.watch.watcher import run_watch_mode

logging.basicConfig(filename=SETTINGS.log_file, level=logging.INFO)
LOGGER = logging.getLogger(__name__)


def _echo_json(data, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(data)


def _is_running() -> bool:
    if not os.path.exists(SETTINGS.pid_file):
        return False
    try:
        with open(SETTINGS.pid_file, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        return psutil.pid_exists(pid)
    except Exception:
        return False


def _current_repo_path() -> str:
    return os.getcwd()


def _db_size_bytes(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    if not os.path.isdir(path):
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _phase(label: str, value: str) -> None:
    click.echo(f"{label:<30} {value}")


@click.group()
def main() -> None:
    """CodeSpine CLI."""


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--full/--incremental", default=False, show_default=True)
@click.option("--deep/--no-deep", default=False, show_default=True, help="Run expensive global analyses.")
@click.option(
    "--embed/--no-embed",
    default=False,
    show_default=True,
    help="Generate vector embeddings (slow if sentence-transformers installed; enables semantic search).",
)
@click.option("--allow-running", is_flag=True, hidden=True, help="Skip MCP running check (used by MCP analyse_project tool).")
def analyse(path: str, full: bool, deep: bool, embed: bool, allow_running: bool) -> None:
    """Index a local Java project (auto-detects workspace / Maven / Gradle layout).

    By default embeddings are skipped for speed. Pass --embed to generate
    vector embeddings for semantic search (requires sentence-transformers).
    """
    if not allow_running and _is_running():
        click.secho("Stop MCP first ('codespine stop') to index.", fg="yellow")
        return

    started = time.perf_counter()
    abs_path = os.path.abspath(path)
    store = GraphStore(read_only=False)
    indexer = JavaIndexer(store)

    # --- Workspace → project → module detection ---
    # Level 1: workspace (e.g. ~/IdeaProjects/) may contain independent projects.
    project_roots = JavaIndexer.detect_projects_in_workspace(abs_path)
    is_workspace = not (len(project_roots) == 1 and project_roots[0] == abs_path)
    if is_workspace:
        click.secho(
            f"Detected workspace with {len(project_roots)} projects in {abs_path}: "
            + ", ".join(os.path.basename(p) for p in project_roots),
            fg="cyan",
        )

    # Level 2: each project may be multi-module (Maven/Gradle).
    # Build a flat list of (module_path, project_id) tuples.
    modules_with_ids: list[tuple[str, str]] = []
    for proj_root in project_roots:
        proj_name = os.path.basename(proj_root)
        module_dirs = JavaIndexer.detect_modules(proj_root)
        is_multi_module = not (len(module_dirs) == 1 and module_dirs[0] == proj_root)
        if is_multi_module:
            module_names = [os.path.basename(m) for m in module_dirs]
            click.secho(
                f"  {proj_name}: {len(module_dirs)} modules – {module_names}",
                fg="cyan",
            )
            for m in module_dirs:
                modules_with_ids.append((m, f"{proj_name}::{os.path.basename(m)}"))
        else:
            modules_with_ids.append((proj_root, proj_name))

    root_basename = os.path.basename(abs_path)

    # Shared progress state (reset per module)
    parse_state = {"shown": False, "indexed": 0, "total": 0, "last_ts": 0.0, "printed_zero": False}
    call_state = {"shown": False, "count": 0, "last_ts": 0.0}

    def _reset_state() -> None:
        for k in list(parse_state):
            parse_state[k] = False if isinstance(parse_state[k], bool) else (0.0 if isinstance(parse_state[k], float) else 0)
        parse_state["last_ts"] = 0.0

    def _progress(event: str, payload: dict) -> None:
        now = time.perf_counter()
        if event == "scan_done":
            _phase("Walking files...", f"{int(payload.get('files_found', 0))} files found")
            return
        if event == "plan_done":
            to_index = int(payload.get("files_to_index", 0))
            deleted = int(payload.get("deleted_files", 0))
            mode = str(payload.get("mode", "incremental"))
            parse_state["total"] = to_index
            _phase("Index mode...", f"{mode} ({to_index} files to index, {deleted} deleted)")
            if to_index == 0:
                _phase("Parsing code...", "0/0")
                parse_state["printed_zero"] = True
            return
        if event == "parse_progress":
            indexed = int(payload.get("indexed", 0))
            total = int(payload.get("total", 0))
            parse_state["indexed"] = indexed
            parse_state["total"] = total
            if total == 0:
                return
            if indexed == total or (now - parse_state["last_ts"]) >= 0.2:
                click.echo(f"\rParsing code...                {indexed}/{total}", nl=False)
                parse_state["shown"] = True
                parse_state["last_ts"] = now
            return
        if event == "resolve_calls_start" and parse_state["shown"]:
            click.echo()
            parse_state["shown"] = False
            _phase("Tracing calls...", "running")
            return
        if event == "resolve_calls_start":
            _phase("Tracing calls...", "running")
            return
        if event == "resolve_calls_progress":
            call_state["count"] = int(payload.get("calls_resolved", 0))
            if (now - call_state["last_ts"]) >= 0.25:
                click.echo(f"\rTracing calls...               {call_state['count']} resolved", nl=False)
                call_state["shown"] = True
                call_state["last_ts"] = now
            return
        if event == "resolve_calls_done":
            if call_state["shown"]:
                click.echo()
            call_state["shown"] = False
            _phase("Tracing calls...", f"{int(payload.get('calls_resolved', 0))} calls resolved")
            return
        if event == "resolve_types_start":
            _phase("Analyzing types...", "running")
            return
        if event == "resolve_types_done":
            _phase("Analyzing types...", f"{int(payload.get('type_relationships', 0))} type relationships")
            return

    # --- Index each module ---
    is_multi = len(modules_with_ids) > 1
    total_files_found = 0
    last_result = None
    for idx, (module_path, project_id) in enumerate(modules_with_ids):
        if is_multi:
            click.echo()
            click.secho(f"[{idx + 1}/{len(modules_with_ids)}] Indexing: {project_id}", fg="cyan")
        _reset_state()
        last_result = indexer.index_project(
            module_path, full=full, progress=_progress, project_id=project_id, embed=embed
        )
        total_files_found += last_result.files_found
        if parse_state["shown"]:
            click.echo()
        if parse_state["total"] == 0 and not parse_state["printed_zero"]:
            _phase("Parsing code...", "0/0")
        elif parse_state["indexed"] < parse_state["total"]:
            _phase("Parsing code...", f"{parse_state['indexed']}/{parse_state['total']}")

    # ── Helper for in-place progress updates ────────────────────────────
    def _live_phase(label: str, status: str) -> None:
        """Overwrite the current line with a status update."""
        click.echo(f"\r{label:<30} {status:<50}", nl=False)

    def _finish_phase(label: str, result: str) -> None:
        """Finalise an in-place phase line and move to the next line."""
        click.echo(f"\r{label:<30} {result:<50}")

    # ── Cross-module call linking ──────────────────────────────────────
    if is_multi and len(modules_with_ids) > 1:
        xmod_label = "Cross-module linking..."
        _live_phase(xmod_label, "running")
        xmod_pids = [pid for _, pid in modules_with_ids]
        xmod_edges = link_cross_module_calls(
            store, project_ids=xmod_pids,
            progress=lambda s: _live_phase(xmod_label, s),
        )
        _finish_phase(xmod_label, f"{xmod_edges} cross-module call edges")
    else:
        _phase("Cross-module linking...", "skipped (single module)")

    communities: list[dict] = []
    flows: list[dict] = []
    dead: list[dict] = []
    coupling_pairs: list[dict] = []

    should_run_deep = deep or total_files_found <= 1200
    if should_run_deep:
        comm_label = "Detecting communities..."
        _live_phase(comm_label, "running")
        communities = detect_communities(
            store,
            progress=lambda s: _live_phase(comm_label, s),
        )
        _finish_phase(comm_label, f"{len(communities)} clusters found")

        flow_label = "Detecting execution flows..."
        _live_phase(flow_label, "running")
        flows = trace_execution_flows(
            store,
            progress=lambda s: _live_phase(flow_label, s),
        )
        _finish_phase(flow_label, f"{len(flows)} processes found")

        dead_label = "Finding dead code..."
        _live_phase(dead_label, "running")
        dead = detect_dead_code(store, limit=500)
        _finish_phase(dead_label, f"{len(dead)} unreachable symbols")

        coup_label = "Analyzing git history..."
        _live_phase(coup_label, "running")
        coupling_root = abs_path
        coupling_project = root_basename if is_multi else (last_result.project_id if last_result else root_basename)
        coupling_pairs = compute_coupling(
            store,
            coupling_root,
            coupling_project,
            months=SETTINGS.default_coupling_months,
            min_strength=SETTINGS.default_min_coupling_strength,
            min_cochanges=SETTINGS.default_min_cochanges,
            progress=lambda s: _live_phase(coup_label, s),
        )
        _finish_phase(coup_label, f"{len(coupling_pairs)} coupled file pairs")
    else:
        _phase("Detecting communities...", "skipped (large repo; rerun with --deep)")
        _phase("Detecting execution flows...", "skipped (large repo; rerun with --deep)")
        _phase("Finding dead code...", "skipped (large repo; rerun with --deep)")
        _phase("Analyzing git history...", "skipped (large repo; rerun with --deep)")

    vector_count = store.query_records(
        """
        MATCH (s:Symbol)
        WHERE s.embedding IS NOT NULL
        RETURN count(s) as count
        """
    )
    embeddings_generated = last_result.embeddings_generated if last_result else 0
    vectors_stored = int(vector_count[0]["count"]) if vector_count else embeddings_generated
    _phase("Generating embeddings...", f"{vectors_stored} vectors stored")

    symbol_count = store.query_records("MATCH (s:Symbol) RETURN count(s) as count")
    edge_count = store.query_records("MATCH ()-[r]->() RETURN count(r) as count")
    symbols = int(symbol_count[0]["count"]) if symbol_count else 0
    edges = int(edge_count[0]["count"]) if edge_count else 0
    elapsed = time.perf_counter() - started

    embed_note = "" if embed else " (no embeddings; rerun with --embed for semantic search)"
    module_info = f"{len(modules_with_ids)} modules/projects, " if is_multi else ""
    click.echo()
    click.secho(
        f"Done in {elapsed:.1f}s - {module_info}{symbols} symbols, {edges} edges, {len(communities)} clusters, {len(flows)} flows{embed_note}",
        fg="green",
    )


@main.command()
@click.argument("query")
@click.option("--k", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def search(query: str, k: int, as_json: bool) -> None:
    """Hybrid search (BM25 + vector + fuzzy + RRF)."""
    store = GraphStore(read_only=True)
    results = hybrid_search(store, query, k=k)
    _echo_json(results, as_json)


@main.command()
@click.argument("query")
@click.option("--max-depth", default=3, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def context(query: str, max_depth: int, as_json: bool) -> None:
    """Get one-shot symbol context: search + impact + community + flows."""
    store = GraphStore(read_only=True)
    result = build_symbol_context(store, query, max_depth=max_depth)
    _echo_json(result, as_json)


@main.command()
@click.argument("symbol")
@click.option("--max-depth", default=4, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def impact(symbol: str, max_depth: int, as_json: bool) -> None:
    """Impact analysis grouped by depth with confidence scores."""
    store = GraphStore(read_only=True)
    result = analyze_impact(store, symbol, max_depth=max_depth)
    _echo_json(result, as_json)


@main.command()
@click.option("--limit", default=200, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def deadcode(limit: int, as_json: bool) -> None:
    """Detect dead code candidates with Java-aware exemptions."""
    store = GraphStore(read_only=True)
    result = detect_dead_code(store, limit=limit)
    _echo_json(result, as_json)


@main.command()
@click.option("--entry", "entry_symbol", default=None)
@click.option("--max-depth", default=6, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def flow(entry_symbol: str | None, max_depth: int, as_json: bool) -> None:
    """Trace execution flows from detected entry points."""
    store = GraphStore(read_only=True)
    result = trace_execution_flows(store, entry_symbol=entry_symbol, max_depth=max_depth)
    _echo_json(result, as_json)


@main.command()
@click.option("--symbol", default=None)
@click.option("--json", "as_json", is_flag=True)
def community(symbol: str | None, as_json: bool) -> None:
    """Detect communities or lookup community for a symbol."""
    store = GraphStore(read_only=False)
    detect_communities(store)
    if symbol:
        _echo_json(symbol_community(store, symbol), as_json)
        return
    communities = store.query_records(
        "MATCH (c:Community) RETURN c.id as id, c.label as label, c.cohesion as cohesion ORDER BY c.cohesion DESC LIMIT 200"
    )
    _echo_json(communities, as_json)


@main.command()
@click.option("--months", default=6, show_default=True, type=int)
@click.option("--min-strength", default=0.3, show_default=True, type=float)
@click.option("--min-cochanges", default=3, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def coupling(months: int, min_strength: float, min_cochanges: int, as_json: bool) -> None:
    """Compute and query git change coupling."""
    store = GraphStore(read_only=False)
    project = store.query_records("MATCH (p:Project) RETURN p.id as id LIMIT 1")
    project_id = project[0]["id"] if project else os.path.basename(os.getcwd())
    compute_coupling(store, os.getcwd(), project_id, months=months, min_strength=min_strength, min_cochanges=min_cochanges)
    result = get_coupling(
        store,
        symbol=None,
        months=months,
        min_strength=min_strength,
        min_cochanges=min_cochanges,
    )
    _echo_json(result, as_json)


@main.command()
@click.option("--path", default=".", show_default=True, type=click.Path(exists=True))
@click.option("--global-interval", default=30, show_default=True, type=int)
def watch(path: str, global_interval: int) -> None:
    """Live re-indexing and periodic global analysis refresh."""
    store = GraphStore(read_only=False)
    run_watch_mode(store, os.path.abspath(path), global_interval=global_interval)


@main.command()
@click.argument("range_spec")
@click.option("--json", "as_json", is_flag=True)
def diff(range_spec: str, as_json: bool) -> None:
    """Compare branches at symbol level: <base>..<head>."""
    if ".." not in range_spec:
        raise click.ClickException("Range must be in format <base>..<head>")
    base_ref, head_ref = range_spec.split("..", 1)
    result = compare_branches(os.getcwd(), base_ref, head_ref)
    _echo_json(result, as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True)
def stats(as_json: bool) -> None:
    """Show per-project and aggregate graph statistics."""
    store = GraphStore(read_only=True)
    projects = store.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path ORDER BY p.id")
    if not projects:
        click.secho("No projects indexed yet. Run 'codespine analyse <path>'.", fg="yellow")
        return

    rows = []
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
        rows.append({
            "project": pid,
            "path": p["path"],
            "files": files[0]["n"] if files else 0,
            "classes": classes[0]["n"] if classes else 0,
            "methods": methods[0]["n"] if methods else 0,
            "calls_out": calls[0]["n"] if calls else 0,
            "embeddings": emb[0]["n"] if emb else 0,
        })

    if as_json:
        _echo_json(rows, as_json=True)
        return

    col_w = max(len(r["project"]) for r in rows)
    header = f"{'Project':<{col_w}}  {'Files':>6}  {'Classes':>8}  {'Methods':>8}  {'Calls':>7}  {'Emb':>6}  Path"
    click.secho(header, fg="cyan")
    click.echo("-" * len(header))
    total_files = total_classes = total_methods = total_calls = total_emb = 0
    for r in rows:
        click.echo(
            f"{r['project']:<{col_w}}  {r['files']:>6}  {r['classes']:>8}  {r['methods']:>8}  {r['calls_out']:>7}  {r['embeddings']:>6}  {r['path']}"
        )
        total_files += r["files"]
        total_classes += r["classes"]
        total_methods += r["methods"]
        total_calls += r["calls_out"]
        total_emb += r["embeddings"]
    if len(rows) > 1:
        click.echo("-" * len(header))
        click.secho(
            f"{'TOTAL':<{col_w}}  {total_files:>6}  {total_classes:>8}  {total_methods:>8}  {total_calls:>7}  {total_emb:>6}",
            fg="green",
        )


@main.command("list")
@click.option("--json", "as_json", is_flag=True)
def list_projects(as_json: bool) -> None:
    """List indexed projects."""
    store = GraphStore(read_only=True)
    projects = store.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path, p.language as language ORDER BY p.id")
    _echo_json(projects, as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True)
def status(as_json: bool) -> None:
    """Show service and database status."""
    running = _is_running()
    pid = None
    if os.path.exists(SETTINGS.pid_file):
        try:
            with open(SETTINGS.pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except Exception:
            pid = None
    payload = {
        "running": running,
        "pid": pid,
        "pid_file": SETTINGS.pid_file,
        "db_path": SETTINGS.db_path,
        "db_size_bytes": _db_size_bytes(SETTINGS.db_path),
        "log_file": SETTINGS.log_file,
    }
    _echo_json(payload, as_json)


@main.command()
@click.argument("query")
@click.option("--json", "as_json", is_flag=True)
def cypher(query: str, as_json: bool) -> None:
    """Run a raw Cypher query against the graph DB."""
    store = GraphStore(read_only=True)
    try:
        result = store.query_records(query)
    except Exception as exc:
        raise click.ClickException(f"Cypher query failed: {exc}") from exc
    _echo_json(result, as_json)


@main.command()
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
def clean(force: bool) -> None:
    """Remove CodeSpine local state (DB/PID/log)."""
    if not force and not click.confirm("Remove local CodeSpine DB, PID, and logs?"):
        click.echo("Aborted.")
        return
    for path in [SETTINGS.pid_file, SETTINGS.log_file, SETTINGS.db_path]:
        if not os.path.exists(path):
            continue
        if os.path.isdir(path):
            import shutil

            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    click.echo("Cleaned CodeSpine local state.")


@main.command("clear-project")
@click.argument("project_id")
@click.option("--allow-running", is_flag=True, hidden=True)
def clear_project_cmd(project_id: str, allow_running: bool) -> None:
    """Remove all indexed data for a single project (clean slate for that project).

    Clears all files, classes, methods, symbols, and the project node itself.
    The meta cache for this project is also removed.
    Run 'codespine analyse <path>' afterwards to re-index from scratch.
    """
    if not allow_running and _is_running():
        click.secho("Stop MCP first ('codespine stop') to modify index.", fg="yellow")
        return
    store = GraphStore(read_only=False)
    recs = store.query_records(
        "MATCH (p:Project) WHERE p.id = $pid RETURN p.id as id, p.path as path",
        {"pid": project_id},
    )
    if not recs:
        click.secho(f"Project '{project_id}' not found in index.", fg="yellow")
        return
    project_path = recs[0].get("path", "")
    store.clear_project(project_id)
    store.execute("MATCH (p:Project) WHERE p.id = $pid DETACH DELETE p", {"pid": project_id})
    meta_path = JavaIndexer._meta_cache_path(project_id)
    if os.path.exists(meta_path):
        try:
            os.remove(meta_path)
        except OSError:
            pass
    click.secho(f"Cleared project '{project_id}' (was at {project_path}).", fg="green")


@main.command("clear-index")
@click.option("--allow-running", is_flag=True, hidden=True)
def clear_index_cmd(allow_running: bool) -> None:
    """Remove ALL indexed data – complete clean slate.

    Deletes every project, file, class, method, symbol, community, and flow
    from the graph. The DB file is kept so the MCP server stays usable.
    Run 'codespine analyse <path>' for each project to re-index from scratch.
    """
    if not allow_running and _is_running():
        click.secho("Stop MCP first ('codespine stop') to modify index.", fg="yellow")
        return
    store = GraphStore(read_only=False)
    projects = store.query_records("MATCH (p:Project) RETURN p.id as id")
    for p in projects:
        store.clear_project(p["id"])
        meta_path = JavaIndexer._meta_cache_path(p["id"])
        if os.path.exists(meta_path):
            try:
                os.remove(meta_path)
            except OSError:
                pass
    store.execute("MATCH (p:Project) DETACH DELETE p")
    store.execute("MATCH (c:Community) DETACH DELETE c")
    store.execute("MATCH (f:Flow) DETACH DELETE f")
    click.secho(f"Cleared {len(projects)} project(s). Index is now empty.", fg="green")


@main.command()
def setup() -> None:
    """Print local setup checks and next steps."""
    checks = {
        "click": False,
        "kuzu": False,
        "tree_sitter_java": False,
        "fastmcp": False,
        "watchfiles": False,
    }
    for mod in list(checks):
        try:
            __import__(mod)
            checks[mod] = True
        except Exception:
            checks[mod] = False
    click.echo("Dependency check:")
    for mod, ok in checks.items():
        click.echo(f"  - {mod}: {'OK' if ok else 'MISSING'}")
    click.echo("\\nRecommended:")
    click.echo("  pip install -e .")
    click.echo("  codespine analyse /path/to/java-project --full")
    click.echo("  codespine search payment --json")


@main.command()
def start() -> None:
    """Launch MCP background server."""
    if _is_running():
        click.secho("CodeSpine already active.", fg="yellow")
        return

    if os.path.exists(SETTINGS.pid_file):
        os.remove(SETTINGS.pid_file)

    proc = subprocess.Popen(
        [sys.executable, "-m", "codespine.cli", "run-mcp"],
        stdout=open(SETTINGS.log_file, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    with open(SETTINGS.pid_file, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    click.secho("CodeSpine MCP active", fg="cyan")


@main.command()
def serve() -> None:
    """Alias for start."""
    start()


@main.command()
def mcp() -> None:
    """Run MCP server in foreground (stdio)."""
    run_mcp()


@main.command()
def stop() -> None:
    """Stop MCP background server."""
    if not os.path.exists(SETTINGS.pid_file):
        click.echo("Nothing to stop.")
        return
    try:
        with open(SETTINGS.pid_file, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Stopped {pid}")
    except Exception:
        click.echo("Stale PID removed")
    finally:
        if os.path.exists(SETTINGS.pid_file):
            os.remove(SETTINGS.pid_file)


@main.command("run-mcp", hidden=True)
def run_mcp() -> None:
    """Run MCP server in stdio mode."""
    store = GraphStore(read_only=True)
    mcp = build_mcp_server(store, repo_path_provider=_current_repo_path)
    mcp.run()


if __name__ == "__main__":
    main()
