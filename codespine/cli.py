from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

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
from codespine.sharding import ShardedGraphStore, ShardRouter
from codespine.diff.branch_diff import compare_branches
from codespine.indexer.engine import JavaIndexer
from codespine.mcp.server import build_mcp_server
from codespine.search.hybrid import hybrid_search
from codespine.watch.watcher import clear_overlay, get_overlay_status, promote_overlay, run_watch_mode

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


def _open_store(read_only: bool = True) -> ShardedGraphStore:
    """Open the sharded store with the backend configured in SETTINGS.

    Every CLI command must go through this helper so the correct backend
    (DuckDB or KùzuDB) is selected transparently.  Direct ``GraphStore(...)``
    calls were tied to the legacy single-DB KùzuDB layout and will fail on
    any machine running the default DuckDB backend with sharded storage.
    """
    return ShardedGraphStore(read_only=read_only)


def _spawn_background_enrichment(path: str) -> bool:
    """Publish the fast index, then enrich it in a detached process."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "codespine.cli", "enrich-background", path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Unable to spawn background enrichment: %s", exc)
        return False


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


def _dead_result_count(dead_result: list[dict] | None) -> int:
    if not dead_result:
        return 0
    return sum(1 for item in dead_result if isinstance(item, dict) and "_stats" not in item)


def _bar(done: int, total: int, width: int = 20) -> str:
    """Return an ASCII progress bar like [████████░░░░]  40%."""
    if total <= 0:
        return f"[{'░' * width}]  ---%"
    frac = min(done / total, 1.0)
    filled = int(width * frac)
    return f"[{'█' * filled}{'░' * (width - filled)}] {int(frac * 100):3d}%"


def _spinner_char() -> str:
    return "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.perf_counter() * 8) % 10]


def _index_shard_group(
    shard_idx: int,
    modules: list[tuple[str, str]],
    sg,
    full: bool,
    embed: bool,
    deadline: float | None,
    output_lock: threading.Lock,
    parallel: bool,
) -> tuple[int, list, int]:
    """Index one group of modules that share a shard.

    Modules within the group are always indexed sequentially (same KùzuDB).
    Multiple groups can run concurrently in different threads when they own
    different shards.

    Returns (total_files_found, all_results, shard_idx).
    """
    results = []
    total_files = 0

    def _locked_echo(*args, **kwargs) -> None:
        """Thread-safe click.echo."""
        with output_lock:
            click.echo(*args, **kwargs)

    def _locked_secho(*args, **kwargs) -> None:
        with output_lock:
            click.secho(*args, **kwargs)

    prefix = f"[S{shard_idx}] " if parallel else ""

    for mod_path, project_id in modules:
        # Per-module progress state (local — no shared mutation).
        parse_state: dict = {
            "shown": False, "indexed": 0, "total": 0,
            "last_ts": 0.0, "printed_zero": False,
            "current_file": "", "elapsed": 0.0,
            "last_done": -1, "frozen_since": 0.0, "stall_warned": False,
        }
        call_state: dict = {"shown": False, "count": 0, "last_ts": 0.0,
                             "started_at": 0.0}
        db_state: dict = {
            "shown": False, "done": 0, "total": 0, "last_ts": 0.0,
            "started_at": 0.0,
        }

        def _progress(event: str, payload: dict) -> None:
            now = time.perf_counter()
            if event == "scan_done":
                with output_lock:
                    _phase(f"{prefix}Walking files...", f"{int(payload.get('files_found', 0))} files found")
                return
            if event == "plan_done":
                to_index = int(payload.get("files_to_index", 0))
                deleted = int(payload.get("deleted_files", 0))
                mode = str(payload.get("mode", "incremental"))
                parse_state["total"] = to_index
                with output_lock:
                    _phase(f"{prefix}Index mode...", f"{mode} ({to_index} files, {deleted} deleted)")
                if to_index == 0:
                    with output_lock:
                        _phase(f"{prefix}Parsing code...", "0/0")
                    parse_state["printed_zero"] = True
                return
            if event == "parse_heartbeat":
                # Fires every 2s from a daemon thread — keeps spinner alive
                # even when all worker threads are busy or one is hanging.
                done    = int(payload.get("done", 0))
                total   = int(payload.get("total", 0))
                current = str(payload.get("current_file", ""))
                elapsed_s = float(payload.get("elapsed", 0.0))
                parse_state["indexed"] = done
                parse_state["total"] = total
                parse_state["current_file"] = current
                parse_state["elapsed"] = elapsed_s
                if total > 0 and not parallel:
                    basename = os.path.basename(current) if current else ""
                    click.echo(
                        f"\r{_spinner_char()} {prefix}Parsing code...   "
                        f"{_bar(done, total)} {done}/{total}  "
                        f"{basename[:38]:<38}  {elapsed_s:.0f}s  ",
                        nl=False,
                    )
                    parse_state["shown"] = True
                    parse_state["last_ts"] = now

                # ── Stall detection ──────────────────────────────────────
                if done == parse_state["last_done"]:
                    if parse_state["frozen_since"] == 0.0:
                        parse_state["frozen_since"] = now
                    stalled_for = now - parse_state["frozen_since"]
                    if stalled_for >= 15.0 and not parse_state["stall_warned"]:
                        parse_state["stall_warned"] = True
                        basename = os.path.basename(current) if current else "unknown"
                        with output_lock:
                            click.echo()  # break out of \r line
                            click.secho(
                                f"  ⚠  Parsing stalled on {basename} for "
                                f"{stalled_for:.0f}s — file may be pathological.\n"
                                f"     Timeout at {os.environ.get('CODESPINE_PARSE_TIMEOUT_SECS', '60')}s. "
                                f"To skip large files: "
                                f"export CODESPINE_MAX_FILE_BYTES=2097152",
                                fg="yellow",
                            )
                else:
                    parse_state["last_done"] = done
                    parse_state["frozen_since"] = 0.0
                    parse_state["stall_warned"] = False
                return
            if event == "parse_progress":
                indexed = int(payload.get("indexed", 0))
                total = int(payload.get("total", 0))
                parse_state["indexed"] = indexed
                parse_state["total"] = total
                # Reset stall tracker on actual progress
                if indexed != parse_state["last_done"]:
                    parse_state["last_done"] = indexed
                    parse_state["frozen_since"] = 0.0
                    parse_state["stall_warned"] = False
                if total == 0:
                    return
                if indexed == total or (now - parse_state["last_ts"]) >= 0.2:
                    if not parallel:
                        # In-place progress bar only makes sense in serial mode.
                        click.echo(
                            f"\r{prefix}Parsing code...   {_bar(indexed, total)} {indexed}/{total}  ",
                            nl=False,
                        )
                    else:
                        with output_lock:
                            click.echo(
                                f"\r{prefix}Parsing {indexed}/{total}  ",
                                nl=False,
                            )
                    parse_state["shown"] = True
                    parse_state["last_ts"] = now
                return
            if event == "db_write_start":
                if parse_state["shown"]:
                    with output_lock:
                        click.echo()
                    parse_state["shown"] = False
                total = int(payload.get("total", 0))
                deleted = int(payload.get("deleted_files", 0))
                db_state["done"] = 0
                db_state["total"] = total
                db_state["started_at"] = now
                status = f"starting ({total} files"
                if deleted:
                    status += f", {deleted} deleted"
                status += ")"
                with output_lock:
                    _phase(f"{prefix}Writing index...", status)
                return
            if event == "db_write_heartbeat":
                done = int(payload.get("done", 0))
                total = int(payload.get("total", 0))
                classes = int(payload.get("classes", 0))
                methods = int(payload.get("methods", 0))
                phase = str(payload.get("phase", "writing"))
                elapsed_s = float(payload.get("elapsed", 0.0))
                db_state["done"] = done
                db_state["total"] = total
                if not parallel:
                    click.echo(
                        f"\r{_spinner_char()} {prefix}Writing index...   "
                        f"{_bar(done, total)} {done}/{total}  "
                        f"{classes} classes / {methods} methods  "
                        f"{phase[:18]:<18} {elapsed_s:.0f}s  ",
                        nl=False,
                    )
                else:
                    with output_lock:
                        click.echo(
                            f"\r{prefix}Writing {done}/{total} "
                            f"({classes} classes, {methods} methods, {elapsed_s:.0f}s)  ",
                            nl=False,
                        )
                db_state["shown"] = True
                db_state["last_ts"] = now
                return
            if event == "db_write_progress":
                done = int(payload.get("done", 0))
                total = int(payload.get("total", 0))
                classes = int(payload.get("classes", 0))
                methods = int(payload.get("methods", 0))
                phase = str(payload.get("phase", "writing"))
                db_state["done"] = done
                db_state["total"] = total
                if total == 0 and done == 0:
                    return
                if done == total or (now - db_state["last_ts"]) >= 0.25:
                    elapsed_s = now - db_state["started_at"]
                    if not parallel:
                        click.echo(
                            f"\r{_spinner_char()} {prefix}Writing index...   "
                            f"{_bar(done, total)} {done}/{total}  "
                            f"{classes} classes / {methods} methods  "
                            f"{phase[:18]:<18} {elapsed_s:.0f}s  ",
                            nl=False,
                        )
                    else:
                        with output_lock:
                            click.echo(
                                f"\r{prefix}Writing {done}/{total} "
                                f"({classes} classes, {methods} methods, {elapsed_s:.0f}s)  ",
                                nl=False,
                            )
                    db_state["shown"] = True
                    db_state["last_ts"] = now
                return
            if event == "db_write_done":
                if db_state["shown"]:
                    with output_lock:
                        click.echo()
                db_state["shown"] = False
                files = int(payload.get("files_indexed", db_state["done"]))
                classes = int(payload.get("classes", 0))
                methods = int(payload.get("methods", 0))
                elapsed_s = float(payload.get("elapsed", 0.0))
                with output_lock:
                    _phase(
                        f"{prefix}Writing index...",
                        f"{files} files, {classes} classes, {methods} methods  ({elapsed_s:.1f}s)",
                    )
                return
            if event in ("resolve_calls_start",):
                if parse_state["shown"] or db_state["shown"]:
                    with output_lock:
                        click.echo()
                    parse_state["shown"] = False
                    db_state["shown"] = False
                call_state["started_at"] = now
                with output_lock:
                    _phase(f"{prefix}Tracing calls...", "starting...")
                return
            if event == "resolve_calls_heartbeat":
                # Fires every 2 s from a daemon thread so the spinner stays
                # alive even when the resolver produces no new edges.
                scanned = int(payload.get("scanned", 0))
                edges   = int(payload.get("edges", 0))
                elapsed_s = float(payload.get("elapsed", 0.0))
                if not parallel:
                    click.echo(
                        f"\r{_spinner_char()} {prefix}Tracing calls...   "
                        f"{edges:>6} resolved / {scanned} scanned  {elapsed_s:.1f}s  ",
                        nl=False,
                    )
                    call_state["shown"] = True
                call_state["last_ts"] = now
                return
            if event == "resolve_calls_progress":
                call_state["count"] = int(payload.get("calls_resolved", 0))
                if (now - call_state["last_ts"]) >= 0.25:
                    elapsed_s = now - call_state["started_at"]
                    if not parallel:
                        click.echo(
                            f"\r{_spinner_char()} {prefix}Tracing calls...   "
                            f"{call_state['count']:>6} resolved  {elapsed_s:.1f}s  ",
                            nl=False,
                        )
                    else:
                        with output_lock:
                            click.echo(
                                f"\r{prefix}Calls: {call_state['count']} ({elapsed_s:.0f}s)  ",
                                nl=False,
                            )
                    call_state["shown"] = True
                    call_state["last_ts"] = now
                return
            if event == "resolve_calls_done":
                if call_state["shown"]:
                    with output_lock:
                        click.echo()
                call_state["shown"] = False
                elapsed_s = (now - call_state["started_at"]) if call_state["started_at"] else 0.0
                n = int(payload.get("calls_resolved", 0))
                suffix = " partial" if payload.get("partial") else ""
                with output_lock:
                    _phase(f"{prefix}Tracing calls...", f"{n} calls resolved{suffix}  ({elapsed_s:.1f}s)")
                return
            if event == "resolve_types_start":
                with output_lock:
                    _phase(f"{prefix}Analyzing types...", "running")
                return
            if event == "resolve_types_done":
                n = int(payload.get("type_relationships", 0))
                suffix = " partial" if payload.get("partial") else ""
                with output_lock:
                    _phase(f"{prefix}Analyzing types...", f"{n} type relationships{suffix}")
                return

        shard_store = sg.shard(project_id)
        indexer = JavaIndexer(shard_store)
        result = indexer.index_project(
            mod_path,
            full=full,
            progress=_progress,
            project_id=project_id,
            embed=embed,
            deadline=deadline,
        )
        results.append(result)
        total_files += result.files_found

        # Flush any dangling progress line.
        if parse_state["shown"] or db_state["shown"]:
            with output_lock:
                click.echo()

    return shard_idx, results, total_files


def _show_shard_topology(as_json: bool) -> None:
    """Display the current shard routing topology and imbalance metrics."""
    router = ShardRouter()
    sg = ShardedGraphStore(read_only=True)
    topology = sg.describe()

    # Gather project → shard mapping from all shards.
    shard_project_counts: dict[int, list[str]] = {i: [] for i in range(router.num_shards)}
    for p in sg.list_project_metadata():
        pid = p.get("id", "")
        idx = router.shard_for(pid)
        shard_project_counts[idx].append(pid)

    counts = [len(v) for v in shard_project_counts.values()]
    total = sum(counts)
    median = sorted(counts)[len(counts) // 2] if counts else 0
    max_count = max(counts) if counts else 0
    imbalance = (max_count / median) if median else 1.0

    if as_json:
        _echo_json({
            "topology": topology,
            "project_distribution": {str(k): v for k, v in shard_project_counts.items()},
            "imbalance_ratio": round(imbalance, 2),
        }, as_json=True)
        return

    click.secho(f"Shard topology ({router.num_shards} shards)", fg="cyan")
    click.echo(f"  Directory : {router.shards_dir}")
    click.echo(f"  Ring size : {len(router._ring)} virtual nodes ({router.num_shards} × {150})")
    click.echo(f"  Projects  : {total} total, imbalance ratio {imbalance:.2f}x")
    click.echo()
    header = f"{'Shard':>6}  {'Projects':>9}  {'DB exists':>10}  Path"
    click.secho(header, fg="cyan")
    click.echo("-" * 60)
    for i, info in enumerate(topology.get("shards", [])):
        plist = shard_project_counts.get(i, [])
        exists_str = "yes" if info.get("exists") else "no"
        click.echo(f"{i:>6}  {len(plist):>9}  {exists_str:>10}  {info.get('db_path', '')}")
        for pid in plist:
            click.echo(f"{'':>6}  {'':>9}  {'':>10}    {pid}")
    if imbalance > 2.0:
        click.secho(
            f"\nWarning: imbalance ratio {imbalance:.1f}x. Consider re-indexing to redistribute projects.",
            fg="yellow",
        )


@click.group()
def main() -> None:
    """CodeSpine CLI."""


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--full/--incremental", default=False, show_default=True)
@click.option("--deep/--no-deep", default=False, show_default=True, help="Run expensive global analyses when used with --complete.")
@click.option(
    "--fast/--complete",
    default=True,
    show_default=True,
    help="Fast mode returns after the core index is queryable; complete mode runs enrichment in the foreground.",
)
@click.option(
    "--budget",
    "budget_seconds",
    default=90.0,
    show_default=True,
    type=float,
    help="Foreground time budget in seconds for fast mode; use 0 to disable the resolver deadline.",
)
@click.option(
    "--incremental-deep",
    is_flag=True,
    default=False,
    help="Force deep analysis even during incremental re-index. Useful after large refactors.",
)
@click.option(
    "--embed/--no-embed",
    default=False,
    show_default=True,
    help="Generate vector embeddings. Off by default so analyse stays fast; rerun with --embed when semantic vectors are needed.",
)
@click.option("--allow-running", is_flag=True, hidden=True, help="Skip MCP running check (used by MCP analyse_project tool).")
def analyse(
    path: str,
    full: bool,
    deep: bool,
    fast: bool,
    budget_seconds: float,
    incremental_deep: bool,
    embed: bool,
    allow_running: bool,
) -> None:
    """Index a local Java project (auto-detects workspace / Maven / Gradle layout).

    Fast mode indexes the core Java graph and returns quickly. Use --complete
    for foreground communities, flows, dead-code, and git-coupling enrichment.
    """
    if not allow_running and _is_running():
        click.secho("Stop MCP first ('codespine stop') to index.", fg="yellow")
        return

    started = time.perf_counter()
    abs_path = os.path.abspath(path)

    if fast and (deep or incremental_deep):
        click.secho(
            "Fast mode runs deep analysis in the background. Use --complete --deep to wait for it.",
            fg="yellow",
        )

    budget_deadline = (
        started + budget_seconds
        if fast and budget_seconds and budget_seconds > 0
        else None
    )

    # Warn about hash fallback early so users know to install [ml]
    if embed:
        from codespine.search.vector import _load_model
        if _load_model() is None:
            click.secho(
                "⚠  sentence-transformers not found — using hash-based embeddings.\n"
                "   For better semantic search: pip install codespine[ml]\n",
                fg="yellow",
            )

    # ShardedGraphStore routes each project to its dedicated DB shard.
    # For single-project analysis this is transparent — shard() always
    # returns a GraphStore pointing to the correct shard path.
    sg = ShardedGraphStore(read_only=False)

    # ── SIGINT handler: flush partial index on Ctrl+C ────────────────────
    # The handler captures `sg` by closure.  On interrupt it snapshots all
    # open shards so `codespine stats` and MCP see the partial result, then
    # calls os._exit(130) to bypass Python cleanup (safe for CLI process).
    # A second Ctrl+C hard-exits immediately.
    _sigint_pressed: list[bool] = [False]
    _old_sigint_handler = signal.getsignal(signal.SIGINT)

    def _sigint_flush(signum: int, frame: object) -> None:  # noqa: ARG001
        if _sigint_pressed[0]:
            os._exit(130)
        _sigint_pressed[0] = True
        # Restore default handler so a second Ctrl+C exits immediately.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        click.secho(
            "\n\n⚠  Interrupted — flushing partial index to read replica…",
            fg="yellow",
        )
        try:
            sg.snapshot_all(background=False)
            click.secho(
                "✓ Partial index saved. Run 'codespine stats' to see what was indexed.",
                fg="yellow",
            )
        except Exception:  # noqa: BLE001
            pass
        os._exit(130)

    signal.signal(signal.SIGINT, _sigint_flush)

    # The indexer is initialised per-module below with the right shard store.
    # We keep a single ShardedGraphStore to fan-out cross-module linking later.

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

    # ── Group modules by target shard ─────────────────────────────────
    # Modules that hash to different shards own separate KùzuDBs and can
    # be indexed in parallel.  Modules in the same shard (same project
    # root for multi-module projects) are always indexed sequentially.
    shard_groups: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for mod_path, pid in modules_with_ids:
        shard_groups[sg.router.shard_for(pid)].append((mod_path, pid))

    is_multi = len(modules_with_ids) > 1
    parallel_mode = len(shard_groups) > 1  # ≥2 shards → true parallelism
    output_lock = threading.Lock()

    if parallel_mode:
        click.secho(
            f"Parallel mode: {len(shard_groups)} shards will be indexed concurrently.",
            fg="cyan",
        )

    # Print which shard each module lands on (multi-module only).
    if is_multi:
        for s_idx, group in sorted(shard_groups.items()):
            for _, pid in group:
                click.secho(f"  {pid:<40} → shard {s_idx}", fg="cyan")

    # ── Dispatch to shards ────────────────────────────────────────────
    total_files_found = 0
    all_results: list = []
    last_result = None

    if parallel_mode:
        max_workers = min(len(shard_groups), 4)
        click.echo()
        futures_map = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="codespine-shard") as ex:
            for s_idx, group in shard_groups.items():
                f = ex.submit(
                    _index_shard_group,
                    s_idx, group, sg, full, embed, budget_deadline, output_lock, True,
                )
                futures_map[f] = s_idx

            for future in as_completed(futures_map):
                s_idx = futures_map[future]
                try:
                    ret_idx, results, n_files = future.result()
                    all_results.extend(results)
                    total_files_found += n_files
                    if results:
                        last_result = results[-1]
                    with output_lock:
                        click.secho(f"  Shard {ret_idx} done ({n_files} files)", fg="green")
                except Exception as exc:  # noqa: BLE001
                    with output_lock:
                        click.secho(f"  Shard {s_idx} FAILED: {exc}", fg="red")
    else:
        # Serial path — single shard (or single module).  Full progress UX.
        only_shard_idx = next(iter(shard_groups))
        only_group = shard_groups[only_shard_idx]
        _, all_results, total_files_found = _index_shard_group(
            only_shard_idx, only_group, sg, full, embed, budget_deadline, output_lock, False,
        )
        if all_results:
            last_result = all_results[-1]

    # ── Helper for in-place progress updates ────────────────────────────
    def _live_phase(label: str, status: str) -> None:
        """Overwrite the current line with a status update."""
        click.echo(f"\r{_spinner_char()} {label:<28} {status:<48}", nl=False)

    def _finish_phase(label: str, result: str) -> None:
        """Finalise an in-place phase line and move to the next line."""
        click.echo(f"\r✓ {label:<28} {result:<48}")

    # For cross-module operations (cross-module linking, deep analysis, stats)
    # we use the shard store for the root project (all modules share one shard).
    root_project_id = last_result.project_id if last_result else root_basename
    root_shard_store = sg.shard(root_project_id)

    # ── Cross-module call linking ──────────────────────────────────────
    if fast and is_multi and len(modules_with_ids) > 1:
        _phase("Cross-module linking...", "skipped (fast mode; use --complete)")
    elif is_multi and len(modules_with_ids) > 1:
        xmod_label = "Cross-module linking..."
        _live_phase(xmod_label, "running")
        xmod_pids = [pid for _, pid in modules_with_ids]
        xmod_edges = link_cross_module_calls(
            root_shard_store, project_ids=xmod_pids,
            progress=lambda s: _live_phase(xmod_label, s),
        )
        _finish_phase(xmod_label, f"{xmod_edges} cross-module call edges")
    else:
        _phase("Cross-module linking...", "skipped (single module)")

    communities: list[dict] = []
    flows: list[dict] = []
    dead: list[dict] = []
    coupling_pairs: list[dict] = []

    should_run_deep = (not fast) and (deep or incremental_deep or total_files_found <= 3000)
    if should_run_deep:
        comm_label = "Detecting communities..."
        _live_phase(comm_label, "running")
        communities = detect_communities(
            root_shard_store,
            progress=lambda s: _live_phase(comm_label, s),
        )
        _finish_phase(comm_label, f"{len(communities)} clusters found")

        flow_label = "Detecting execution flows..."
        _live_phase(flow_label, "running")
        flows = trace_execution_flows(
            root_shard_store,
            progress=lambda s: _live_phase(flow_label, s),
        )
        _finish_phase(flow_label, f"{len(flows)} processes found")

        dead_label = "Finding dead code..."
        _live_phase(dead_label, "running")
        dead = detect_dead_code(root_shard_store, limit=500)
        _finish_phase(dead_label, f"{_dead_result_count(dead)} unreachable symbols")

        coup_label = "Analyzing git history..."
        _live_phase(coup_label, "running")
        root_shard_store.clear_coupling()
        coupling_root = abs_path
        coupling_project = root_basename if is_multi else (last_result.project_id if last_result else root_basename)
        coupling_pairs = compute_coupling(
            root_shard_store,
            coupling_root,
            coupling_project,
            days=SETTINGS.default_coupling_days,
            min_strength=SETTINGS.default_min_coupling_strength,
            min_cochanges=SETTINGS.default_min_cochanges,
            progress=lambda s: _live_phase(coup_label, s),
        )
        _finish_phase(coup_label, f"{len(coupling_pairs)} coupled file pairs")
    elif fast:
        _phase("Detecting communities...", "queued in background")
        _phase("Detecting execution flows...", "queued in background")
        _phase("Finding dead code...", "queued in background")
        _phase("Analyzing git history...", "queued in background")
    else:
        # Run lightweight versions of flow tracing and dead code from the call
        # graph already built — no community detection or coupling (those are
        # genuinely expensive).  This gives partial results without --deep.
        _phase("Detecting communities...", "skipped (large repo; rerun with --deep)")

        flow_label = "Detecting execution flows..."
        _live_phase(flow_label, "running (lightweight)")
        try:
            flows = trace_execution_flows(root_shard_store, max_depth=3)
        except Exception:
            flows = []
        _finish_phase(flow_label, f"{len(flows)} flows (lightweight; rerun with --deep for full)")

        dead_label = "Finding dead code..."
        _live_phase(dead_label, "running (lightweight)")
        try:
            dead = detect_dead_code(root_shard_store, limit=100)
        except Exception:
            dead = []
        _finish_phase(dead_label, f"{_dead_result_count(dead)} candidates (lightweight; rerun with --deep for full)")

        _phase("Analyzing git history...", "skipped (large repo; rerun with --deep)")

    # Summary queries are best-effort: a translator miss or a transient
    # DB error must never throw away a successful index.
    def _safe_count(query: str) -> int:
        try:
            rows = root_shard_store.query_records(query)
            return int(rows[0]["count"]) if rows else 0
        except Exception as exc:  # noqa: BLE001 - summary stats are non-critical
            click.secho(f"   (summary stat unavailable: {exc})", fg="yellow")
            return 0

    embeddings_generated = last_result.embeddings_generated if last_result else 0
    vectors_stored = _safe_count(
        """
        MATCH (s:Symbol)
        WHERE s.embedding IS NOT NULL
        RETURN count(s) as count
        """
    ) or embeddings_generated
    _phase("Generating embeddings...", f"{vectors_stored} vectors stored")

    symbols = _safe_count("MATCH (s:Symbol) RETURN count(s) as count")
    edges = _safe_count("MATCH ()-[r]->() RETURN count(r) as count")
    elapsed = time.perf_counter() - started

    if not embed:
        embed_note = " (no embeddings; rerun with --embed for semantic search)"
    elif _load_model() is None:
        embed_note = " (hash embeddings; pip install codespine[ml] for better search)"
    else:
        embed_note = ""
    module_info = f"{len(modules_with_ids)} modules/projects, " if is_multi else ""
    click.echo()
    click.secho(
        f"Done in {elapsed:.1f}s - {module_info}{symbols} symbols, {edges} edges, {len(communities)} clusters, {len(flows)} flows{embed_note}",
        fg="green",
    )

    # Detect unresolved imports → hint about unindexed sibling projects.
    # This is useful, but it is still another global query, so fast mode leaves
    # it out of the foreground path.
    if not fast:
        try:
            unresolved = JavaIndexer.detect_unresolved_imports(root_shard_store)
            if unresolved:
                click.echo()
                click.secho("⚠  Unresolved imports — consider indexing these projects:", fg="yellow")
                for pkg, samples in sorted(unresolved.items())[:8]:
                    click.echo(f"   {pkg}  (e.g. {samples[0]})")
        except Exception:
            pass  # best-effort

    # Publish a read replica so MCP and read-only CLI commands (search, stats…)
    # run against an isolated snapshot rather than competing with the write
    # process's buffer pool.  Snapshot all open shards concurrently.
    snap_label = "Publishing read replica..."
    for store in sg.open_shards():
        recycle = getattr(store, "_recycle_conn", None)
        if callable(recycle):
            recycle()
    if fast and _spawn_background_enrichment(abs_path):
        _phase(snap_label, "core snapshot now; enrichment continues in background")
    else:
        _live_phase(snap_label, "copying")
        sg.snapshot_all(background=False)
        _finish_phase(snap_label, "MCP will reload automatically")

    # Restore original SIGINT handler now that we've finished cleanly.
    signal.signal(signal.SIGINT, _old_sigint_handler)


@main.command("publish-snapshot", hidden=True)
def publish_snapshot() -> None:
    """Publish sharded read replicas for a recently completed analyse run."""
    sg = ShardedGraphStore(read_only=False)
    sg.snapshot_all(background=False)


@main.command("enrich-background", hidden=True)
@click.argument("path", type=click.Path(exists=True))
def enrich_background(path: str) -> None:
    """Run expensive post-index graph enrichment outside the analyse foreground."""
    abs_path = os.path.abspath(path)
    LOGGER.info("Background enrichment starting for %s", abs_path)

    project_roots = JavaIndexer.detect_projects_in_workspace(abs_path)
    modules_with_ids: list[tuple[str, str]] = []
    for proj_root in project_roots:
        proj_name = os.path.basename(proj_root)
        module_dirs = JavaIndexer.detect_modules(proj_root)
        is_multi_module = not (len(module_dirs) == 1 and module_dirs[0] == proj_root)
        if is_multi_module:
            for m in module_dirs:
                modules_with_ids.append((m, f"{proj_name}::{os.path.basename(m)}"))
        else:
            modules_with_ids.append((proj_root, proj_name))

    root_basename = os.path.basename(abs_path)
    root_project_id = modules_with_ids[-1][1] if modules_with_ids else root_basename
    is_multi = len(modules_with_ids) > 1
    xmod_pids = [pid for _, pid in modules_with_ids]

    sg = ShardedGraphStore(read_only=False)
    root_shard_store = sg.shard(root_project_id)

    try:
        # Publish the fast core graph first so MCP/search can use it while the
        # more expensive enrichment keeps working.
        sg.snapshot_all(background=False)

        if is_multi and len(xmod_pids) > 1:
            xmod_edges = link_cross_module_calls(
                root_shard_store,
                project_ids=xmod_pids,
                progress=lambda s: LOGGER.info("Cross-module linking: %s", s),
            )
            LOGGER.info("Background cross-module linking wrote %d edges", xmod_edges)

        communities = detect_communities(
            root_shard_store,
            progress=lambda s: LOGGER.info("Community detection: %s", s),
        )
        LOGGER.info("Background community detection found %d clusters", len(communities))

        flows = trace_execution_flows(
            root_shard_store,
            progress=lambda s: LOGGER.info("Execution flow tracing: %s", s),
        )
        LOGGER.info("Background flow tracing found %d flows", len(flows))

        dead = detect_dead_code(root_shard_store, limit=500)
        LOGGER.info("Background dead-code scan found %d candidates", _dead_result_count(dead))

        root_shard_store.clear_coupling()
        coupling_project = root_basename if is_multi else root_project_id
        coupling_pairs = compute_coupling(
            root_shard_store,
            abs_path,
            coupling_project,
            days=SETTINGS.default_coupling_days,
            min_strength=SETTINGS.default_min_coupling_strength,
            min_cochanges=SETTINGS.default_min_cochanges,
            progress=lambda s: LOGGER.info("Git coupling: %s", s),
        )
        LOGGER.info("Background coupling analysis found %d pairs", len(coupling_pairs))

        sg.snapshot_all(background=False)
        LOGGER.info("Background enrichment finished for %s", abs_path)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Background enrichment failed for %s: %s", abs_path, exc)
        raise


@main.command()
@click.argument("query")
@click.option("--k", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def search(query: str, k: int, as_json: bool) -> None:
    """Hybrid search (BM25 + vector + fuzzy + RRF)."""
    store = _open_store(read_only=True)
    results = hybrid_search(store, query, k=k)
    _echo_json(results, as_json)


@main.command()
@click.argument("query")
@click.option("--max-depth", default=3, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def context(query: str, max_depth: int, as_json: bool) -> None:
    """Get one-shot symbol context: search + impact + community + flows."""
    store = _open_store(read_only=True)
    result = build_symbol_context(store, query, max_depth=max_depth)
    _echo_json(result, as_json)


@main.command()
@click.argument("symbol")
@click.option("--max-depth", default=4, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def impact(symbol: str, max_depth: int, as_json: bool) -> None:
    """Impact analysis grouped by depth with confidence scores."""
    store = _open_store(read_only=True)
    result = analyze_impact(store, symbol, max_depth=max_depth)
    _echo_json(result, as_json)


@main.command()
@click.option("--limit", default=200, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def deadcode(limit: int, as_json: bool) -> None:
    """Detect dead code candidates with Java-aware exemptions."""
    store = _open_store(read_only=True)
    result = detect_dead_code(store, limit=limit)
    _echo_json(result, as_json)


@main.command()
@click.option("--entry", "entry_symbol", default=None)
@click.option("--max-depth", default=6, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def flow(entry_symbol: str | None, max_depth: int, as_json: bool) -> None:
    """Trace execution flows from detected entry points."""
    store = _open_store(read_only=True)
    result = trace_execution_flows(store, entry_symbol=entry_symbol, max_depth=max_depth)
    _echo_json(result, as_json)


@main.command()
@click.option("--symbol", default=None)
@click.option("--json", "as_json", is_flag=True)
def community(symbol: str | None, as_json: bool) -> None:
    """Detect communities or lookup community for a symbol."""
    store = _open_store(read_only=False)
    detect_communities(store)
    if symbol:
        _echo_json(symbol_community(store, symbol), as_json)
        return
    communities = store.query_records(
        "MATCH (c:Community) RETURN c.id as id, c.label as label, c.cohesion as cohesion ORDER BY c.cohesion DESC LIMIT 200"
    )
    _echo_json(communities, as_json)


@main.command()
@click.option("--days", default=5, show_default=True, type=int)
@click.option("--min-strength", default=0.3, show_default=True, type=float)
@click.option("--min-cochanges", default=3, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def coupling(days: int, min_strength: float, min_cochanges: int, as_json: bool) -> None:
    """Compute and query git change coupling."""
    store = _open_store(read_only=False)
    project = store.query_records("MATCH (p:Project) RETURN p.id as id LIMIT 1")
    project_id = project[0]["id"] if project else os.path.basename(os.getcwd())
    compute_coupling(store, os.getcwd(), project_id, days=days, min_strength=min_strength, min_cochanges=min_cochanges)
    result = get_coupling(
        store,
        symbol=None,
        days=days,
        min_strength=min_strength,
        min_cochanges=min_cochanges,
    )
    _echo_json(result, as_json)


@main.command()
@click.option("--path", default=".", show_default=True, type=click.Path(exists=True))
@click.option("--global-interval", default=30, show_default=True, type=int)
@click.option(
    "--overlay-debounce-ms",
    default=SETTINGS.default_overlay_debounce_ms,
    show_default=True,
    type=int,
)
@click.option("--promote-on-commit/--no-promote-on-commit", default=True, show_default=True)
def watch(path: str, global_interval: int, overlay_debounce_ms: int, promote_on_commit: bool) -> None:
    """Live re-indexing and periodic global analysis refresh."""
    store = _open_store(read_only=False)
    run_watch_mode(
        store,
        os.path.abspath(path),
        global_interval=global_interval,
        overlay_debounce_ms=overlay_debounce_ms,
        promote_on_commit=promote_on_commit,
    )


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
@click.option("--shards", "show_shards", is_flag=True, help="Show shard topology and load distribution.")
def stats(as_json: bool, show_shards: bool) -> None:
    """Show per-project and aggregate graph statistics."""
    if show_shards:
        _show_shard_topology(as_json)
        return

    # Fan-out across all shards so stats covers every project in the cluster.
    sg = ShardedGraphStore(read_only=True)
    all_projects_meta = sg.list_project_metadata()

    # For detailed stats we need the per-project shard store.
    def _project_store(pid: str):
        return sg.shard(pid)

    if not all_projects_meta:
        click.secho("No projects indexed yet. Run 'codespine analyse <path>'.", fg="yellow")
        return

    def _stat_count(store, query: str, params: dict) -> int:
        """Run a stats count query — returns 0 on any failure."""
        try:
            rows = store.query_records(query, params)
            return int(rows[0]["n"]) if rows else 0
        except Exception as exc:  # noqa: BLE001
            click.secho(f"   (stat unavailable: {exc})", fg="yellow")
            return 0

    rows = []
    for p in all_projects_meta:
        pid = p["id"]
        # Route each query to the project's owning shard.
        ps = _project_store(pid)
        n_files = _stat_count(
            ps,
            "MATCH (f:File) WHERE f.project_id = $pid RETURN count(f) as n",
            {"pid": pid},
        )
        n_classes = _stat_count(
            ps,
            """
            MATCH (f:File) WHERE f.project_id = $pid
            WITH f
            MATCH (c:Class) WHERE c.file_id = f.id
            RETURN count(c) as n
            """,
            {"pid": pid},
        )
        n_methods = _stat_count(
            ps,
            """
            MATCH (f:File) WHERE f.project_id = $pid
            WITH f
            MATCH (c:Class) WHERE c.file_id = f.id
            WITH c
            MATCH (c)-[:HAS_METHOD]->(m:Method)
            RETURN count(m) as n
            """,
            {"pid": pid},
        )
        n_calls = _stat_count(
            ps,
            """
            MATCH (f:File) WHERE f.project_id = $pid
            WITH f
            MATCH (c:Class) WHERE c.file_id = f.id
            WITH c
            MATCH (c)-[:HAS_METHOD]->(m:Method)-[:CALLS]->()
            RETURN count(*) as n
            """,
            {"pid": pid},
        )
        n_emb = _stat_count(
            ps,
            """
            MATCH (f:File) WHERE f.project_id = $pid
            WITH f
            MATCH (s:Symbol) WHERE s.file_id = f.id AND s.embedding IS NOT NULL
            RETURN count(s) as n
            """,
            {"pid": pid},
        )
        rows.append({
            "project": pid,
            "path": p["path"],
            "shard": sg.router.shard_for(pid),
            "files": n_files,
            "classes": n_classes,
            "methods": n_methods,
            "calls_out": n_calls,
            "embeddings": n_emb,
        })

    if as_json:
        _echo_json(rows, as_json=True)
        return

    col_w = max(len(r["project"]) for r in rows)
    header = f"{'Project':<{col_w}}  {'Shard':>5}  {'Files':>6}  {'Classes':>8}  {'Methods':>8}  {'Calls':>7}  {'Emb':>6}  Path"
    click.secho(header, fg="cyan")
    click.echo("-" * len(header))
    total_files = total_classes = total_methods = total_calls = total_emb = 0
    for r in rows:
        click.echo(
            f"{r['project']:<{col_w}}  {r.get('shard', 0):>5}  {r['files']:>6}  {r['classes']:>8}  {r['methods']:>8}  {r['calls_out']:>7}  {r['embeddings']:>6}  {r['path']}"
        )
        total_files += r["files"]
        total_classes += r["classes"]
        total_methods += r["methods"]
        total_calls += r["calls_out"]
        total_emb += r["embeddings"]
    if len(rows) > 1:
        click.echo("-" * len(header))
        click.secho(
            f"{'TOTAL':<{col_w}}  {'':>5}  {total_files:>6}  {total_classes:>8}  {total_methods:>8}  {total_calls:>7}  {total_emb:>6}",
            fg="green",
        )


@main.command("list")
@click.option("--json", "as_json", is_flag=True)
def list_projects(as_json: bool) -> None:
    """List indexed projects."""
    store = _open_store(read_only=True)
    projects = store.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path, p.language as language ORDER BY p.id")
    _echo_json(projects, as_json)


@main.command()
@click.option("--json", "as_json", is_flag=True)
def status(as_json: bool) -> None:
    """Show service and database status.

    Quick reference for MCP server management:
      codespine start    – launch background MCP server
      codespine stop     – stop background MCP server
      codespine status   – this command
      codespine mcp      – run MCP in foreground (stdio, for IDE integration)
    """
    running = _is_running()
    pid = None
    if os.path.exists(SETTINGS.pid_file):
        try:
            with open(SETTINGS.pid_file, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
        except Exception:
            pid = None
    store = _open_store(read_only=True)
    overlay = get_overlay_status(store)

    # Check for stale PID file
    stale_pid = pid is not None and not running
    has_snapshot = os.path.exists(SETTINGS.db_snapshot_path)

    payload = {
        "running": running,
        "pid": pid,
        "stale_pid": stale_pid,
        "pid_file": SETTINGS.pid_file,
        "db_path": SETTINGS.db_path,
        "db_size_bytes": _db_size_bytes(SETTINGS.db_path),
        "read_replica": SETTINGS.db_snapshot_path if has_snapshot else None,
        "read_replica_size_bytes": _db_size_bytes(SETTINGS.db_snapshot_path) if has_snapshot else 0,
        "log_file": SETTINGS.log_file,
        "overlay_dir": SETTINGS.overlay_dir,
        "overlay_projects": overlay,
    }
    if as_json:
        _echo_json(payload, True)
    else:
        _echo_json(payload, True)
        if stale_pid:
            click.secho(f"\n⚠  Stale PID file found (PID {pid} not running). Run 'codespine stop' to clean up.", fg="yellow")
        if not running:
            click.echo("\nTo start:  codespine start")
            click.echo("For IDE:   codespine mcp  (stdio mode)")
        else:
            click.echo(f"\nMCP server running (PID {pid}). Stop with: codespine stop")


@main.command("overlay-status")
@click.option("--project", default=None)
@click.option("--json", "as_json", is_flag=True)
def overlay_status_cmd(project: str | None, as_json: bool) -> None:
    """Show dirty overlay status by project/module."""
    store = _open_store(read_only=True)
    _echo_json(get_overlay_status(store, project=project), as_json)


@main.command("overlay-clear")
@click.option("--project", default=None)
@click.option("--json", "as_json", is_flag=True)
def overlay_clear_cmd(project: str | None, as_json: bool) -> None:
    """Clear dirty overlay data without touching the committed base index."""
    store = _open_store(read_only=False)
    result = {"cleared": clear_overlay(store, project=project)}
    _echo_json(result, as_json)


@main.command("overlay-promote")
@click.option("--project", default=None)
@click.option("--json", "as_json", is_flag=True)
def overlay_promote_cmd(project: str | None, as_json: bool) -> None:
    """Promote dirty overlay changes into the committed base index now."""
    store = _open_store(read_only=False)
    result = {"promoted": promote_overlay(store, project=project, require_head_change=False)}
    _echo_json(result, as_json)


@main.command()
@click.argument("query")
@click.option("--json", "as_json", is_flag=True)
def cypher(query: str, as_json: bool) -> None:
    """Run a raw Cypher query against the graph DB."""
    store = _open_store(read_only=True)
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
    for path in [SETTINGS.pid_file, SETTINGS.log_file, SETTINGS.db_path, SETTINGS.overlay_dir]:
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
    try:
        store = _open_store(read_only=False)
        recs = store.query_records(
            "MATCH (p:Project) WHERE p.id = $pid RETURN p.id as id, p.path as path",
            {"pid": project_id},
        )
    except Exception as exc:
        click.secho(f"DB is corrupted ({exc}). Use 'codespine force-reset' to wipe all data.", fg="red")
        return
    if not recs:
        click.secho(f"Project '{project_id}' not found in index.", fg="yellow")
        return
    project_path = recs[0].get("path", "")
    try:
        store.clear_analysis_artifacts()
        store.clear_project(project_id)
    except Exception as exc:
        click.secho(f"DB write failed ({exc}). Use 'codespine force-reset' to recover.", fg="red")
        return
    store.overlay_store.clear_project(project_id)
    meta_path = JavaIndexer._meta_cache_path(project_id)
    if os.path.exists(meta_path):
        try:
            os.remove(meta_path)
        except OSError:
            pass
    # Update the read replica so read-only callers (stats, MCP) see the change.
    store.snapshot_to_read_replica()
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
    try:
        store = _open_store(read_only=False)
        projects = store.query_records("MATCH (p:Project) RETURN p.id as id")
    except Exception:
        # DB is corrupted — can't even open it.  Force-delete everything.
        click.secho("DB is corrupted. Running force-reset instead...", fg="yellow")
        removed = ShardedGraphStore(read_only=False).force_delete_all_data()
        click.secho(f"Force-reset complete. {len(removed)} path(s) removed. Index is now empty.", fg="green")
        return
    try:
        store.rebuild_empty_db()
    except Exception as exc:
        # rebuild_empty_db failed even with fallbacks — force-delete.
        click.secho(f"rebuild failed ({exc}). Running force-reset...", fg="yellow")
        store.force_delete_all_data()
        click.secho("Force-reset complete. Index is now empty.", fg="green")
        return
    store.overlay_store.clear_all()
    for p in projects:
        meta_path = JavaIndexer._meta_cache_path(p["id"])
        if os.path.exists(meta_path):
            try:
                os.remove(meta_path)
            except OSError:
                pass
    # Publish an empty read replica so that read-only callers (stats, MCP)
    # immediately see the cleared state and the MCP daemon hot-reloads.
    store.snapshot_to_read_replica()
    click.secho(f"Cleared {len(projects)} project(s). Index is now empty.", fg="green")


@main.command("force-reset")
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
def force_reset_cmd(force: bool) -> None:
    """Emergency reset: delete ALL CodeSpine data files without touching the DB engine.

    Use this when the buffer pool is exhausted and normal reset/clear commands
    also fail with OOM.  This bypasses Kuzu entirely by removing data files
    from disk, including the DB, read replica, overlay, meta cache, and
    embedding cache.

    After running this, restart the MCP server and re-index your projects.
    """
    if not force and not click.confirm(
        "This will DELETE all CodeSpine data (DB, overlay, caches). Continue?"
    ):
        click.echo("Aborted.")
        return
    removed = ShardedGraphStore(read_only=False).force_delete_all_data()
    if removed:
        for p in removed:
            click.echo(f"  removed: {p}")
        click.secho(f"\nForce-reset complete. {len(removed)} path(s) removed.", fg="green")
        click.echo("Next: restart MCP ('codespine stop && codespine start') and re-index.")
    else:
        click.secho("Nothing to remove — already clean.", fg="yellow")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def guide(as_json: bool) -> None:
    """Show what CodeSpine can do: tool catalog, workflows, and tips."""
    from codespine.guide import GUIDE_SECTIONS, format_guide_terminal

    if as_json:
        _echo_json({"sections": GUIDE_SECTIONS}, as_json=True)
    else:
        click.echo(format_guide_terminal())


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
    click.echo("Core dependencies:")
    for mod, ok in checks.items():
        click.echo(f"  - {mod}: {'OK' if ok else 'MISSING'}")
    # Check optional ML dependencies
    try:
        from sentence_transformers import SentenceTransformer
        click.echo("  - sentence-transformers: OK (semantic embeddings active)")
    except ImportError:
        click.secho("  - sentence-transformers: NOT INSTALLED (hash fallback; install for better search)", fg="yellow")
    click.echo("\nRecommended setup:")
    click.echo("  pip install -e '.[full]'                # core + ML + community detection")
    click.echo("  pip install -e '.[ml]'                  # just ML embeddings")
    click.echo("\nQuick start:")
    click.echo("  codespine analyse /path/to/java-project --full")
    click.echo("  codespine start                         # launch MCP server")
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


@main.command("install-model")
def install_model() -> None:
    """Download and cache the sentence-transformers embedding model.

    Requires 'pip install codespine[ml]'. The model is downloaded once and
    cached locally; subsequent analyse runs use the cache without network access.
    """
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401
    except ImportError:
        click.secho(
            "sentence-transformers is not installed.\n"
            "Run: pip install codespine[ml]",
            fg="red",
        )
        return

    model_name = SETTINGS.embedding_model
    click.secho(f"Downloading model '{model_name}' …", fg="cyan")
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(model_name)
        # Run a tiny inference to confirm the model is usable.
        _ = model.encode(["hello world"])
        click.secho(f"✓ Model '{model_name}' ready. Semantic search is now enabled.", fg="green")
    except Exception as exc:
        click.secho(f"✗ Failed to load model: {exc}", fg="red")


@main.command("run-mcp", hidden=True)
def run_mcp() -> None:
    """Run MCP server in stdio mode."""
    store = _open_store(read_only=True)
    mcp = build_mcp_server(store, repo_path_provider=_current_repo_path)
    mcp.run()


if __name__ == "__main__":
    main()
