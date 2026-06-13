from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import click
import psutil
from click.core import ParameterSource

from codespine.analysis.community import detect_communities, symbol_community
from codespine.analysis.context import build_symbol_context
from codespine.analysis.coupling import compute_coupling, get_coupling
from codespine.analysis.crossmodule import link_cross_module_calls
from codespine.analysis.deadcode import detect_dead_code
from codespine.analysis.flow import trace_execution_flows
from codespine.analysis.impact import analyze_impact
from codespine.config import SETTINGS
from codespine.graphrag import evaluate_graph_rag_suite, graph_rag_answer
from codespine.sharding import ShardedGraphStore, ShardRouter
from codespine.diff.branch_diff import compare_branches
from codespine.health import index_health, project_health, smoke_test_index
from codespine.indexer.engine import JavaIndexer
from codespine.mcp.server import build_mcp_server
from codespine.project_state import (
    derive_project_status,
    list_project_states,
    load_project_state,
    record_snapshot_success,
    repair_hint_for,
    snapshot_info,
    synthetic_project_state,
    update_project_state,
)
from codespine.search.hybrid import hybrid_search
from codespine.tasks import active_tasks, create_task, finish_task, list_tasks, update_task
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


def _discover_modules(abs_path: str) -> tuple[list[str], list[tuple[str, str]], bool]:
    project_roots = JavaIndexer.detect_projects_in_workspace(abs_path)
    is_workspace = not (len(project_roots) == 1 and project_roots[0] == abs_path)
    modules_with_ids: list[tuple[str, str]] = []
    for proj_root in project_roots:
        proj_name = os.path.basename(proj_root)
        module_dirs = JavaIndexer.detect_modules(proj_root)
        is_multi_module = not (len(module_dirs) == 1 and module_dirs[0] == proj_root)
        if is_multi_module:
            for module_path in module_dirs:
                modules_with_ids.append((module_path, f"{proj_name}::{os.path.basename(module_path)}"))
        else:
            modules_with_ids.append((proj_root, proj_name))
    return project_roots, modules_with_ids, is_workspace


def _set_project_states(modules_with_ids: list[tuple[str, str]], **fields: object) -> None:
    for module_path, project_id in modules_with_ids:
        update_project_state(project_id, path=module_path, **fields)


def _record_snapshot_for_projects(modules_with_ids: list[tuple[str, str]]) -> None:
    for module_path, project_id in modules_with_ids:
        update_project_state(project_id, path=module_path)
        record_snapshot_success(project_id)


def _resolve_repair_target(target: str) -> tuple[str, list[tuple[str, str]], dict[str, object]]:
    if os.path.exists(target):
        abs_path = os.path.abspath(target)
        _, modules_with_ids, _ = _discover_modules(abs_path)
        states = [load_project_state(pid) for _, pid in modules_with_ids]
        primary = states[0] if states else {}
        return abs_path, modules_with_ids, primary

    for state in list_project_states():
        if state.get("project_id") == target:
            path = str(state.get("path") or "")
            if not path:
                raise click.ClickException(f"Project '{target}' has no recorded path; run a full re-index.")
            abs_path = os.path.abspath(path)
            _, modules_with_ids, _ = _discover_modules(abs_path) if os.path.exists(abs_path) else ([abs_path], [(abs_path, target)], False)
            return abs_path, modules_with_ids, state

    abs_target = os.path.abspath(target)
    for state in list_project_states():
        if os.path.abspath(str(state.get("path") or "")) == abs_target:
            _, modules_with_ids, _ = _discover_modules(abs_target) if os.path.exists(abs_target) else ([abs_target], [(abs_target, str(state.get("project_id") or os.path.basename(abs_target)))], False)
            return abs_target, modules_with_ids, state

    raise click.ClickException(f"Could not resolve '{target}' to an indexed project or path.")


def _start_repair(target: str, force_full: bool = False) -> dict[str, object]:
    abs_path, modules_with_ids, primary_state = _resolve_repair_target(target)
    state = primary_state or (load_project_state(modules_with_ids[0][1]) if modules_with_ids else {})
    snap = snapshot_info(modules_with_ids[0][1]) if modules_with_ids else {}
    project_status = derive_project_status(state, snap)

    if force_full or project_status == "repair_required":
        task_id = _spawn_background_full_repair(abs_path)
        mode = "full"
    elif project_status == "partial":
        task_id = _spawn_background_continuation(abs_path, trigger="repair-core")
        mode = "core"
    elif project_status == "degraded":
        task_id = _spawn_background_enrichment(abs_path, trigger="repair-deep")
        mode = "deep"
    elif project_status == "enriching":
        return {
            "ok": True,
            "status": project_status,
            "path": abs_path,
            "note": "Deep enrichment is already running.",
        }
    else:
        return {
            "ok": True,
            "status": project_status,
            "path": abs_path,
            "note": "Project is already healthy.",
        }

    if task_id is None:
        raise click.ClickException("Unable to start repair task.")
    return {
        "ok": True,
        "mode": mode,
        "path": abs_path,
        "task_id": task_id,
        "project_ids": [pid for _, pid in modules_with_ids],
    }


def _spawn_background_enrichment(path: str, *, trigger: str = "analyse") -> str | None:
    """Publish the fast index, then enrich it in a detached process."""
    _, modules_with_ids, _ = _discover_modules(os.path.abspath(path))
    project_id = modules_with_ids[0][1] if len(modules_with_ids) == 1 else None
    task_id = create_task(
        "enrichment",
        "Background graph enrichment",
        path=path,
        project_id=project_id,
        metadata={"trigger": trigger},
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "codespine.cli", "enrich-background", path, "--task-id", task_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        update_task(task_id, status="running", phase="spawned", pid=proc.pid)
        _set_project_states(
            modules_with_ids,
            deep_state="running",
            last_task_id=task_id,
            repair_hint="",
            last_error="",
        )
        return task_id
    except Exception as exc:  # noqa: BLE001
        finish_task(task_id, "failed", str(exc))
        LOGGER.warning("Unable to spawn background enrichment: %s", exc)
        return None


def _spawn_background_continuation(path: str, *, trigger: str = "analyse-budget") -> str | None:
    """Continue a budget-paused core index in a detached process."""
    _, modules_with_ids, _ = _discover_modules(os.path.abspath(path))
    project_id = modules_with_ids[0][1] if len(modules_with_ids) == 1 else None
    task_id = create_task(
        "indexing",
        "Background core indexing",
        path=path,
        project_id=project_id,
        metadata={"trigger": trigger},
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "codespine.cli", "continue-background", path, "--task-id", task_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        update_task(task_id, status="running", phase="spawned", pid=proc.pid)
        _set_project_states(
            modules_with_ids,
            core_state="indexing",
            last_task_id=task_id,
            repair_hint="",
        )
        return task_id
    except Exception as exc:  # noqa: BLE001
        finish_task(task_id, "failed", str(exc))
        LOGGER.warning("Unable to spawn background continuation: %s", exc)
        return None


def _spawn_background_full_repair(path: str) -> str | None:
    abs_path = os.path.abspath(path)
    _, modules_with_ids, _ = _discover_modules(abs_path)
    project_id = modules_with_ids[0][1] if len(modules_with_ids) == 1 else None
    task_id = create_task(
        "repair",
        "Background full repair",
        path=abs_path,
        project_id=project_id,
        metadata={"trigger": "repair", "mode": "full"},
        repair_hint=repair_hint_for(path=abs_path, full=True),
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "codespine.cli", "repair-background", abs_path, "--mode", "full", "--task-id", task_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.getcwd(),
            env=os.environ.copy(),
        )
        update_task(task_id, status="running", phase="spawned", pid=proc.pid)
        _set_project_states(
            modules_with_ids,
            core_state="indexing",
            deep_state="queued",
            last_task_id=task_id,
            repair_hint="",
            last_error="",
        )
        return task_id
    except Exception as exc:  # noqa: BLE001
        finish_task(task_id, "failed", str(exc), repair_hint=repair_hint_for(path=abs_path, full=True))
        LOGGER.warning("Unable to spawn background full repair: %s", exc)
        return None


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


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


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
            if event == "budget_exhausted":
                files = int(payload.get("files_indexed", 0))
                total = int(payload.get("total", 0))
                phase = str(payload.get("phase", "indexing"))
                with output_lock:
                    _phase(
                        f"{prefix}Foreground budget...",
                        f"paused during {phase} ({files}/{total} files); continuing in background",
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
@click.option(
    "--deep/--no-deep",
    default=True,
    show_default=True,
    help="Enable expensive global analyses. By default they continue in the background after the core graph is ready; pass --complete --deep to wait for them.",
)
@click.option(
    "--fast/--complete",
    default=False,
    show_default=True,
    help="Fast mode allows a budgeted partial core index; complete mode waits for the full core graph to be validated before returning.",
)
@click.option(
    "--budget",
    "budget_seconds",
    default=90.0,
    show_default=True,
    type=float,
    help="Foreground time budget in seconds for fast mode; use 0 to disable the budget.",
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
@click.pass_context
def analyse(
    ctx: click.Context,
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

    By default CodeSpine completes the core graph in the foreground, publishes
    a validated read replica, and continues deep enrichment in the background.
    Use --fast for a budgeted partial core index, or --complete --deep to wait
    for deep enrichment before returning.
    """
    if not allow_running and _is_running():
        click.secho("Stop MCP first ('codespine stop') to index.", fg="yellow")
        return

    started = time.perf_counter()
    abs_path = os.path.abspath(path)
    deep_explicit = ctx.get_parameter_source("deep") != ParameterSource.DEFAULT
    deep_enabled = bool(deep or incremental_deep)
    wait_for_deep = (not fast) and deep_enabled and deep_explicit
    if fast and deep_enabled:
        click.secho(
            "Fast mode may return a partial core graph. Deep analysis will wait for core completion and continue in the background.",
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
            _record_snapshot_for_projects(modules_with_ids)
            _set_project_states(
                modules_with_ids,
                core_state="partial",
                deep_state="queued" if deep_enabled else "idle",
                last_error="Indexing was interrupted before the core graph completed.",
                repair_hint=repair_hint_for(path=abs_path),
            )
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
    project_roots, modules_with_ids, is_workspace = _discover_modules(abs_path)
    if is_workspace:
        click.secho(
            f"Detected workspace with {len(project_roots)} projects in {abs_path}: "
            + ", ".join(os.path.basename(p) for p in project_roots),
            fg="cyan",
        )

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
            

    root_basename = os.path.basename(abs_path)
    _set_project_states(
        modules_with_ids,
        core_state="indexing",
        deep_state="queued" if deep_enabled else "idle",
        last_error="",
        repair_hint="",
    )

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
    deep_error: Exception | None = None

    should_run_deep = wait_for_deep
    if should_run_deep:
        try:
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
        except Exception as exc:  # noqa: BLE001
            deep_error = exc
            click.echo()
            click.secho(
                f"Deep enrichment...          failed ({str(exc)[:140]}); publishing the validated core graph and marking the project degraded.",
                fg="yellow",
            )
            LOGGER.exception("Foreground deep enrichment failed for %s: %s", abs_path, exc)
    elif deep_enabled:
        _phase("Detecting communities...", "queued in background")
        _phase("Detecting execution flows...", "queued in background")
        _phase("Finding dead code...", "queued in background")
        _phase("Analyzing git history...", "queued in background")
    else:
        _phase("Detecting communities...", "disabled (--no-deep)")
        _phase("Detecting execution flows...", "disabled (--no-deep)")
        _phase("Finding dead code...", "disabled (--no-deep)")
        _phase("Analyzing git history...", "disabled (--no-deep)")

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
    core_partial = any(bool(getattr(result, "partial", False)) for result in all_results)

    if core_partial:
        _phase("Index self-test...", "deferred (core index is partial)")
        _phase("Index health...", "deferred (run 'codespine repair' after core completes)")
    else:
        self_test = smoke_test_index(root_shard_store)
        if self_test.get("ok"):
            _phase("Index self-test...", "passed")
        else:
            failed = [c.get("name", "unknown") for c in self_test.get("checks", []) if not c.get("ok")]
            click.secho(
                f"Index self-test...          failed ({', '.join(failed)})",
                fg="yellow",
            )

        health_anomalies: list[dict] = []
        try:
            for _, pid in modules_with_ids:
                health_anomalies.extend(project_health(sg.shard(pid), pid).get("anomalies", []))
            critical = sum(1 for a in health_anomalies if a.get("severity") == "critical")
            if critical:
                click.secho(
                    f"Index health...             {critical} critical anomaly(s); run 'codespine health'",
                    fg="yellow",
                )
            elif health_anomalies:
                _phase("Index health...", f"{len(health_anomalies)} warning(s); run 'codespine health'")
            else:
                _phase("Index health...", "no anomalies")
        except Exception as exc:  # noqa: BLE001 - post-index diagnostics are best-effort
            click.secho(f"Index health...             unavailable ({exc})", fg="yellow")

    elapsed = time.perf_counter() - started

    if not embed:
        embed_note = " (no embeddings; rerun with --embed for semantic search)"
    elif _load_model() is None:
        embed_note = " (hash embeddings; pip install codespine[ml] for better search)"
    else:
        embed_note = ""
    module_info = f"{len(modules_with_ids)} modules/projects, " if is_multi else ""
    outcome_prefix = "Partial core snapshot" if core_partial else "Done"
    click.echo()
    click.secho(
        f"{outcome_prefix} in {elapsed:.1f}s - {module_info}{symbols} symbols, {edges} edges, {len(communities)} clusters, {len(flows)} flows{embed_note}",
        fg="yellow" if core_partial else "green",
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
    if fast and core_partial:
        _live_phase(snap_label, "copying partial core")
        sg.snapshot_all(background=False)
        _finish_phase(snap_label, "partial index visible")
        _record_snapshot_for_projects(modules_with_ids)
        _set_project_states(
            modules_with_ids,
            core_state="partial",
            deep_state="queued" if deep_enabled else "idle",
            last_error="Foreground budget exhausted before the core graph completed.",
            repair_hint=repair_hint_for(path=abs_path),
        )
        if _spawn_background_continuation(abs_path):
            _phase("Background indexing...", "core indexing continues; run 'codespine background'")
        else:
            _phase("Background indexing...", "not started; rerun 'codespine analyse' to continue")
    else:
        _live_phase(snap_label, "copying")
        sg.snapshot_all(background=False)
        _finish_phase(snap_label, "MCP will reload automatically")
        _record_snapshot_for_projects(modules_with_ids)
        _set_project_states(
            modules_with_ids,
            core_state="ready",
            deep_state="failed" if deep_error else ("ready" if should_run_deep else ("running" if deep_enabled else "idle")),
            last_error=str(deep_error) if deep_error else "",
            repair_hint=repair_hint_for(path=abs_path) if deep_error else "",
        )
        if deep_error:
            _phase("Repair hint...", repair_hint_for(path=abs_path))
        elif deep_enabled and not should_run_deep:
            if _spawn_background_enrichment(abs_path):
                _phase("Background enrichment...", "deep analysis continues; run 'codespine background'")
            else:
                _set_project_states(
                    modules_with_ids,
                    deep_state="failed",
                    last_error="Unable to start background enrichment.",
                    repair_hint=repair_hint_for(path=abs_path),
                )
                _phase("Background enrichment...", "not started; run 'codespine repair'")

    # Restore original SIGINT handler now that we've finished cleanly.
    signal.signal(signal.SIGINT, _old_sigint_handler)


@main.command("publish-snapshot", hidden=True)
def publish_snapshot() -> None:
    """Publish sharded read replicas for a recently completed analyse run."""
    sg = ShardedGraphStore(read_only=False)
    sg.snapshot_all(background=False)


@main.command("enrich-background", hidden=True)
@click.argument("path", type=click.Path(exists=True))
@click.option("--task-id", default=None, hidden=True)
def enrich_background(path: str, task_id: str | None) -> None:
    """Run expensive post-index graph enrichment outside the analyse foreground."""
    abs_path = os.path.abspath(path)
    LOGGER.info("Background enrichment starting for %s", abs_path)
    _, modules_with_ids, _ = _discover_modules(abs_path)
    project_id = modules_with_ids[0][1] if len(modules_with_ids) == 1 else None
    if task_id is None:
        task_id = create_task(
            "enrichment",
            "Background graph enrichment",
            path=abs_path,
            project_id=project_id,
        )
    update_task(
        task_id,
        status="running",
        phase="starting",
        pid=os.getpid(),
        detail="Preparing background enrichment",
        project_id=project_id,
    )
    _set_project_states(
        modules_with_ids,
        deep_state="running",
        last_task_id=task_id,
        last_error="",
        repair_hint="",
    )

    def _task_phase(phase: str, detail: str = "", progress: float | None = None) -> None:
        update_task(task_id, status="running", phase=phase, detail=detail, progress=progress)
        if detail:
            LOGGER.info("%s: %s", phase, detail)

    root_basename = os.path.basename(abs_path)
    root_project_id = modules_with_ids[-1][1] if modules_with_ids else root_basename
    is_multi = len(modules_with_ids) > 1
    xmod_pids = [pid for _, pid in modules_with_ids]

    sg = ShardedGraphStore(read_only=False)
    root_shard_store = sg.shard(root_project_id)

    try:
        # Publish the fast core graph first so MCP/search can use it while the
        # more expensive enrichment keeps working.
        _task_phase("publishing core snapshot", "Making the fast index visible", 0.05)
        sg.snapshot_all(background=False)
        _record_snapshot_for_projects(modules_with_ids)

        if is_multi and len(xmod_pids) > 1:
            _task_phase("cross-module linking", "Linking calls across indexed modules", 0.20)
            xmod_edges = link_cross_module_calls(
                root_shard_store,
                project_ids=xmod_pids,
                progress=lambda s: LOGGER.info("Cross-module linking: %s", s),
            )
            LOGGER.info("Background cross-module linking wrote %d edges", xmod_edges)

        _task_phase("community detection", "Detecting graph communities", 0.40)
        communities = detect_communities(
            root_shard_store,
            progress=lambda s: LOGGER.info("Community detection: %s", s),
        )
        LOGGER.info("Background community detection found %d clusters", len(communities))

        _task_phase("execution flows", "Tracing execution flows", 0.60)
        flows = trace_execution_flows(
            root_shard_store,
            progress=lambda s: LOGGER.info("Execution flow tracing: %s", s),
        )
        LOGGER.info("Background flow tracing found %d flows", len(flows))

        _task_phase("dead code", "Finding dead-code candidates", 0.75)
        dead = detect_dead_code(root_shard_store, limit=500)
        LOGGER.info("Background dead-code scan found %d candidates", _dead_result_count(dead))

        _task_phase("git coupling", "Analyzing git co-change history", 0.85)
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

        _task_phase("publishing enriched snapshot", "Publishing enriched graph", 0.95)
        sg.snapshot_all(background=False)
        _record_snapshot_for_projects(modules_with_ids)
        _set_project_states(
            modules_with_ids,
            core_state="ready",
            deep_state="ready",
            last_error="",
            repair_hint="",
            last_task_id=task_id,
        )
        finish_task(task_id, "succeeded", "Background enrichment complete")
        LOGGER.info("Background enrichment finished for %s", abs_path)
    except Exception as exc:  # noqa: BLE001
        repair_hint = repair_hint_for(path=abs_path)
        has_core_snapshot = any(
            bool(load_project_state(pid).get("last_good_snapshot_at")) or snapshot_info(pid).get("write_db_valid")
            for _, pid in modules_with_ids
        )
        if has_core_snapshot:
            _set_project_states(
                modules_with_ids,
                core_state="ready",
                deep_state="failed",
                last_error=str(exc),
                repair_hint=repair_hint,
                last_task_id=task_id,
            )
        else:
            _set_project_states(
                modules_with_ids,
                core_state="repair_required",
                deep_state="failed",
                last_error=str(exc),
                repair_hint=repair_hint,
                last_task_id=task_id,
            )
        finish_task(task_id, "failed", str(exc), repair_hint=repair_hint)
        LOGGER.exception("Background enrichment failed for %s: %s", abs_path, exc)
        raise


@main.command("continue-background", hidden=True)
@click.argument("path", type=click.Path(exists=True))
@click.option("--task-id", default=None, hidden=True)
def continue_background(path: str, task_id: str | None) -> None:
    """Continue core indexing after a foreground budget pause."""
    abs_path = os.path.abspath(path)
    _, modules_with_ids, _ = _discover_modules(abs_path)
    project_id = modules_with_ids[0][1] if len(modules_with_ids) == 1 else None
    if task_id is None:
        task_id = create_task(
            "indexing",
            "Background core indexing",
            path=abs_path,
            project_id=project_id,
        )
    update_task(
        task_id,
        status="running",
        phase="core indexing",
        pid=os.getpid(),
        progress=0.10,
        detail="Continuing analyse without the foreground budget",
        project_id=project_id,
    )
    _set_project_states(
        modules_with_ids,
        core_state="indexing",
        last_task_id=task_id,
        repair_hint="",
    )
    try:
        with open(SETTINGS.log_file, "a", encoding="utf-8") as log:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codespine.cli",
                    "analyse",
                    abs_path,
                    "--allow-running",
                ],
                cwd=os.getcwd(),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if proc.returncode == 0:
            finish_task(task_id, "succeeded", "Background core indexing complete")
            return
        repair_hint = repair_hint_for(path=abs_path)
        next_state = "partial" if any(load_project_state(pid).get("last_good_snapshot_at") for _, pid in modules_with_ids) else "repair_required"
        _set_project_states(
            modules_with_ids,
            core_state=next_state,
            last_error=f"Background analyse exited with code {proc.returncode}",
            repair_hint=repair_hint,
            last_task_id=task_id,
        )
        finish_task(
            task_id,
            "failed",
            f"Background analyse exited with code {proc.returncode}",
            repair_hint=repair_hint,
        )
        raise click.ClickException(f"Background analyse exited with code {proc.returncode}")
    except Exception as exc:  # noqa: BLE001
        repair_hint = repair_hint_for(path=abs_path)
        next_state = "partial" if any(load_project_state(pid).get("last_good_snapshot_at") for _, pid in modules_with_ids) else "repair_required"
        _set_project_states(
            modules_with_ids,
            core_state=next_state,
            last_error=str(exc),
            repair_hint=repair_hint,
            last_task_id=task_id,
        )
        finish_task(task_id, "failed", str(exc), repair_hint=repair_hint)
        raise


@main.command("repair-background", hidden=True)
@click.argument("path", type=click.Path(exists=True))
@click.option("--mode", type=click.Choice(["full"]), default="full", hidden=True)
@click.option("--task-id", default=None, hidden=True)
def repair_background(path: str, mode: str, task_id: str | None) -> None:
    """Run a full repair in the background."""
    abs_path = os.path.abspath(path)
    _, modules_with_ids, _ = _discover_modules(abs_path)
    project_id = modules_with_ids[0][1] if len(modules_with_ids) == 1 else None
    repair_hint = repair_hint_for(path=abs_path, full=True)
    if task_id is None:
        task_id = create_task(
            "repair",
            "Background full repair",
            path=abs_path,
            project_id=project_id,
            repair_hint=repair_hint,
        )
    update_task(
        task_id,
        status="running",
        phase="full repair",
        pid=os.getpid(),
        progress=0.10,
        detail="Rebuilding the core graph and republishing a validated snapshot",
        project_id=project_id,
    )
    _set_project_states(
        modules_with_ids,
        core_state="indexing",
        deep_state="queued",
        last_task_id=task_id,
        repair_hint="",
        last_error="",
    )
    try:
        with open(SETTINGS.log_file, "a", encoding="utf-8") as log:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codespine.cli",
                    "analyse",
                    abs_path,
                    "--full",
                    "--allow-running",
                ],
                cwd=os.getcwd(),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if proc.returncode == 0:
            finish_task(task_id, "succeeded", "Background full repair complete")
            return
        _set_project_states(
            modules_with_ids,
            core_state="repair_required",
            deep_state="failed",
            last_task_id=task_id,
            last_error=f"Background full repair exited with code {proc.returncode}",
            repair_hint=repair_hint,
        )
        finish_task(
            task_id,
            "failed",
            f"Background full repair exited with code {proc.returncode}",
            repair_hint=repair_hint,
        )
        raise click.ClickException(f"Background full repair exited with code {proc.returncode}")
    except Exception as exc:  # noqa: BLE001
        _set_project_states(
            modules_with_ids,
            core_state="repair_required",
            deep_state="failed",
            last_task_id=task_id,
            last_error=str(exc),
            repair_hint=repair_hint,
        )
        finish_task(task_id, "failed", str(exc), repair_hint=repair_hint)
        raise


@main.command("repair")
@click.argument("target")
@click.option("--full", "force_full", is_flag=True, help="Force a full core re-index instead of retrying only the failed phase.")
@click.option("--json", "as_json", is_flag=True)
def repair_cmd(target: str, force_full: bool, as_json: bool) -> None:
    """Repair a degraded, partial, or missing project snapshot."""
    payload = _start_repair(target, force_full=force_full)
    if as_json:
        _echo_json(payload, True)
        return
    if payload.get("task_id"):
        click.secho(f"Started {payload.get('mode')} repair for {payload.get('path')}", fg="green")
        click.echo(f"Task: {payload.get('task_id')}")
        click.echo("Use 'codespine background' to watch progress.")
    else:
        click.echo(str(payload.get("note") or payload.get("status") or "No repair needed."))


@main.command()
@click.argument("query")
@click.option("--k", default=20, show_default=True, type=int)
@click.option("--project", default=None)
@click.option("--explain", is_flag=True, help="Return retrieval provenance and match reasons.")
@click.option("--json", "as_json", is_flag=True)
def search(query: str, k: int, project: str | None, explain: bool, as_json: bool) -> None:
    """Hybrid search (BM25 + vector + fuzzy + RRF)."""
    store = _open_store(read_only=True)
    results = hybrid_search(store, query, k=k, project=project, explain=explain)
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
@click.argument("question")
@click.option("--project", default=None)
@click.option("--max-depth", default=3, show_default=True, type=int)
@click.option("--k", default=5, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def answer(question: str, project: str | None, max_depth: int, k: int, as_json: bool) -> None:
    """GraphRAG answer surface with reranked evidence, citations, confidence, and observability."""
    store = _open_store(read_only=True)
    result = graph_rag_answer(store, question, project=project, max_depth=max_depth, k=k)
    _echo_json(result, as_json)


@main.command("answer-eval")
@click.pass_context
@click.option("--suite", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--project", default=None)
@click.option("--max-depth", default=3, show_default=True, type=int)
@click.option("--k", default=5, show_default=True, type=int)
@click.option("--min-average-score", default=80.0, show_default=True, type=float)
@click.option("--min-case-score", default=70.0, show_default=True, type=float)
@click.option("--min-pass-rate", default=1.0, show_default=True, type=float)
@click.option("--json", "as_json", is_flag=True)
def answer_eval_cmd(
    ctx: click.Context,
    suite: str,
    project: str | None,
    max_depth: int,
    k: int,
    min_average_score: float,
    min_case_score: float,
    min_pass_rate: float,
    as_json: bool,
) -> None:
    """Run GraphRAG regression cases and enforce quality gates."""
    with open(suite, "r", encoding="utf-8") as handle:
        suite_payload = json.load(handle)
    store = _open_store(read_only=True)
    gates: dict[str, object] = {}
    if ctx.get_parameter_source("min_average_score") == click.core.ParameterSource.COMMANDLINE:
        gates["min_average_score"] = min_average_score
    if ctx.get_parameter_source("min_case_score") == click.core.ParameterSource.COMMANDLINE:
        gates["min_case_score"] = min_case_score
    if ctx.get_parameter_source("min_pass_rate") == click.core.ParameterSource.COMMANDLINE:
        gates["min_pass_rate"] = min_pass_rate
    result = evaluate_graph_rag_suite(
        store,
        suite_payload,
        project=project,
        max_depth=max_depth,
        k=k,
        gates=gates,
    )
    if as_json:
        _echo_json(result, True)
    else:
        summary = result.get("summary", {})
        gates = result.get("quality_gates", {})
        click.echo(
            f"GraphRAG regression suite: {summary.get('passed_count', 0)}/{summary.get('case_count', 0)} passed, "
            f"average score {float(summary.get('average_score', 0.0)):.2f}, min score {float(summary.get('min_score', 0.0)):.2f}"
        )
        if gates.get("passed"):
            click.secho("Quality gates passed.", fg="green")
        else:
            click.secho("Quality gates failed.", fg="red")
            for violation in gates.get("violations", []):
                click.echo(f"  - {violation.get('message', '')}")
    if not result.get("quality_gates", {}).get("passed"):
        raise SystemExit(1)


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
    summaries = _project_summaries()
    if not summaries:
        click.secho("No projects indexed yet. Run 'codespine analyse <path>'.", fg="yellow")
        return

    rows = [
        {
            "project": item["id"],
            "path": item.get("path"),
            "shard": item.get("shard"),
            "files": item.get("files", 0),
            "classes": item.get("classes", 0),
            "methods": item.get("methods", 0),
            "calls_out": item.get("calls", 0),
            "embeddings": item.get("embeddings", 0),
            "project_state": item.get("project_state"),
        }
        for item in summaries
    ]

    if as_json:
        _echo_json(rows, as_json=True)
        return

    col_w = max(len(r["project"]) for r in rows)
    header = f"{'Project':<{col_w}}  {'State':<15}  {'Shard':>5}  {'Files':>6}  {'Classes':>8}  {'Methods':>8}  {'Calls':>7}  {'Emb':>6}  Path"
    click.secho(header, fg="cyan")
    click.echo("-" * len(header))
    total_files = total_classes = total_methods = total_calls = total_emb = 0
    for r in rows:
        click.echo(
            f"{r['project']:<{col_w}}  {str(r.get('project_state') or '-'): <15}  {r.get('shard', 0):>5}  {r['files']:>6}  {r['classes']:>8}  {r['methods']:>8}  {r['calls_out']:>7}  {r['embeddings']:>6}  {r['path']}"
        )
        total_files += r["files"]
        total_classes += r["classes"]
        total_methods += r["methods"]
        total_calls += r["calls_out"]
        total_emb += r["embeddings"]
    if len(rows) > 1:
        click.echo("-" * len(header))
        click.secho(
            f"{'TOTAL':<{col_w}}  {'':<15}  {'':>5}  {total_files:>6}  {total_classes:>8}  {total_methods:>8}  {total_calls:>7}  {total_emb:>6}",
            fg="green",
        )


@main.command("health")
@click.option("--json", "as_json", is_flag=True)
def health_cmd(as_json: bool) -> None:
    """Show index health, coverage, and anomaly checks."""
    store = _open_store(read_only=True)
    try:
        result = index_health(store)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(f"Unable to read index health: {exc}") from exc

    if as_json:
        _echo_json(result, True)
        return

    projects = result.get("projects", [])
    summary = result.get("summary", {})
    if not projects:
        click.secho("No projects indexed yet. Run 'codespine analyse <path>'.", fg="yellow")
        return

    click.secho(
        f"Index health: {summary.get('project_count', 0)} project(s), "
        f"{summary.get('anomaly_count', 0)} anomaly(s), "
        f"{summary.get('critical_count', 0)} critical",
        fg="cyan",
    )
    click.echo()
    col_w = max(len(str(p.get("project_id", ""))) for p in projects)
    header = f"{'Project':<{col_w}}  {'Shard':>5}  {'Files':>6}  {'Methods':>8}  {'Calls':>8}  {'Coverage':>9}  Health"
    click.secho(header, fg="cyan")
    click.echo("-" * len(header))
    for project in projects:
        anomalies = project.get("anomalies", [])
        health = "ok"
        if any(a.get("severity") == "critical" for a in anomalies):
            health = "critical"
        elif anomalies:
            health = "warning"
        click.echo(
            f"{project.get('project_id', ''):<{col_w}}  "
            f"{str(project.get('shard') if project.get('shard') is not None else '-') :>5}  "
            f"{int(project.get('files', 0)):>6}  "
            f"{int(project.get('methods', 0)):>8}  "
            f"{int(project.get('calls', 0)):>8}  "
            f"{float(project.get('call_edge_coverage', 0.0)) * 100:>8.1f}%  "
            f"{health}"
        )
        for anomaly in anomalies:
            click.secho(
                f"  - {anomaly.get('severity', 'warning')}: {anomaly.get('message', '')}",
                fg="yellow" if anomaly.get("severity") != "critical" else "red",
            )

    integrity = result.get("graph_integrity", {})
    issues = integrity.get("issues", []) if isinstance(integrity, dict) else []
    if issues:
        click.echo()
        click.secho("Graph integrity:", fg="cyan")
        for issue in issues:
            fg = "red" if issue.get("severity") == "critical" else "yellow"
            click.secho(f"  - {issue.get('severity', 'warning')}: {issue.get('message', '')}", fg=fg)


@main.command("self-test")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def self_test_cmd(ctx: click.Context, as_json: bool) -> None:
    """Run smoke queries that catch schema and translator regressions."""
    store = _open_store(read_only=True)
    result = smoke_test_index(store)
    if as_json:
        _echo_json(result, True)
    else:
        if result.get("ok"):
            click.secho("Index self-test passed.", fg="green")
        else:
            click.secho("Index self-test failed.", fg="red")
            for check in result.get("checks", []):
                if not check.get("ok"):
                    click.echo(f"  - {check.get('name')}: {check.get('error', '')}")
    if not result.get("ok"):
        ctx.exit(1)


@main.command("list")
@click.option("--json", "as_json", is_flag=True)
def list_projects(as_json: bool) -> None:
    """List indexed projects."""
    projects = [
        {
            "id": item.get("id"),
            "path": item.get("path"),
            "state": item.get("project_state"),
            "core_state": item.get("core_state"),
            "deep_state": item.get("deep_state"),
        }
        for item in _project_summaries()
    ]
    _echo_json(projects, as_json)


@main.command("tasks")
@click.option("--all", "include_finished", is_flag=True, help="Include completed and failed tasks.")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def tasks_cmd(include_finished: bool, limit: int, as_json: bool) -> None:
    """Show CodeSpine background tasks."""
    _show_background_tasks(include_finished=include_finished, limit=limit, as_json=as_json)


@main.command("background")
@click.option(
    "--all/--running-only",
    "include_finished",
    default=True,
    show_default=True,
    help="Include completed and failed tasks, or show only currently running work.",
)
@click.option("--limit", default=20, show_default=True, type=int)
@click.option("--json", "as_json", is_flag=True)
def background_cmd(include_finished: bool, limit: int, as_json: bool) -> None:
    """Show background task progress."""
    _show_background_tasks(include_finished=include_finished, limit=limit, as_json=as_json)


def _show_background_tasks(include_finished: bool, limit: int, as_json: bool) -> None:
    tasks = list_tasks(include_finished=include_finished, limit=limit)
    if as_json:
        _echo_json(tasks, True)
        return
    if not tasks:
        if include_finished:
            click.echo("No background tasks recorded.")
        else:
            click.echo("No running background tasks.")
        return
    now = time.time()
    header = f"{'ID':<12}  {'Status':<10}  {'Result':<10}  {'Progress':>8}  {'Phase':<24}  {'Age':>7}  Path"
    click.secho(header, fg="cyan")
    click.echo("-" * len(header))
    for task in tasks:
        started = task.get("started_at")
        age = _format_elapsed(now - float(started)) if started else "-"
        path = task.get("path") or ""
        phase = str(task.get("last_phase") or task.get("phase") or "")[:24]
        result_status = str(task.get("result_status") or "-")
        progress = task.get("progress")
        if isinstance(progress, (int, float)):
            progress_str = f"{min(max(float(progress), 0.0), 1.0) * 100:.0f}%"
        else:
            progress_str = "-"
        click.echo(
            f"{str(task.get('id', '')):<12}  "
            f"{str(task.get('status', '')):<10}  "
            f"{result_status:<10}  "
            f"{progress_str:>8}  "
            f"{phase:<24}  "
            f"{age:>7}  "
            f"{path}"
        )
        detail = task.get("detail")
        if detail:
            click.echo(f"{'':<12}  {'':<10}  {'':<10}  {'':>8}  {str(detail)[:80]}")
        repair_hint = task.get("repair_hint")
        if repair_hint and str(task.get("status")) == "failed":
            click.echo(f"{'':<12}  {'':<10}  {'':<10}  {'':>8}  repair: {repair_hint}")


def _project_summaries() -> list[dict]:
    sg = ShardedGraphStore(read_only=True)
    meta_by_id = {project.get("id", ""): project for project in sg.list_project_metadata() if project.get("id")}
    state_by_id = {item.get("project_id", ""): item for item in list_project_states() if item.get("project_id")}
    project_ids = sorted({pid for pid in list(meta_by_id) + list(state_by_id) if pid})
    out: list[dict] = []
    for pid in project_ids:
        project = meta_by_id.get(pid, {})
        state = state_by_id.get(pid) or synthetic_project_state(pid, path=project.get("path", ""))
        snap = snapshot_info(pid, sg.router)
        shard_store = sg.shard(pid)

        def _count(query: str) -> int:
            try:
                rows = shard_store.query_records(query, {"pid": pid})
                return int(rows[0]["n"]) if rows else 0
            except Exception:
                return 0

        out.append(
            {
                "id": pid,
                "path": state.get("path") or project.get("path"),
                "shard": sg.router.shard_for(pid),
                "files": _count("MATCH (f:File) WHERE f.project_id = $pid RETURN count(f) as n"),
                "classes": _count(
                    "MATCH (c:Class), (f:File) WHERE c.file_id = f.id AND f.project_id = $pid RETURN count(c) as n"
                ),
                "methods": _count(
                    "MATCH (m:Method), (c:Class), (f:File) "
                    "WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid RETURN count(m) as n"
                ),
                "calls": _count(
                    "MATCH (ma:Method)-[:CALLS]->(mb:Method), (ca:Class), (fa:File) "
                    "WHERE ma.class_id = ca.id AND ca.file_id = fa.id AND fa.project_id = $pid RETURN count(*) as n"
                ),
                "embeddings": _count(
                    "MATCH (s:Symbol), (f:File) "
                    "WHERE s.file_id = f.id AND f.project_id = $pid AND s.embedding IS NOT NULL RETURN count(s) as n"
                ),
                "project_state": derive_project_status(state, snap),
                "core_state": state.get("core_state"),
                "deep_state": state.get("deep_state"),
                "last_error": state.get("last_error"),
                "repair_hint": state.get("repair_hint"),
                "last_good_snapshot_at": state.get("last_good_snapshot_at"),
                "snapshot_valid": snap.get("snapshot_valid"),
                "write_db_valid": snap.get("write_db_valid"),
            }
        )
    return out


def _ui_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CodeSpine Index Explorer</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; color: #202124; background: #f7f9fb; }
    header { padding: 24px 28px 18px; background: #ffffff; border-bottom: 1px solid #d9e1ea; }
    h1 { margin: 0 0 6px; font-size: 26px; font-weight: 720; letter-spacing: 0; }
    main { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(320px, 0.8fr); gap: 18px; padding: 18px 28px 32px; }
    section { min-width: 0; }
    .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
    input { width: min(420px, 100%); padding: 10px 12px; border: 1px solid #c8d3df; border-radius: 6px; font-size: 14px; }
    button { padding: 10px 14px; border: 1px solid #1b6f79; border-radius: 6px; background: #1b6f79; color: white; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; background: white; border: 1px solid #d9e1ea; }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #e7edf3; font-size: 14px; vertical-align: top; }
    th { background: #eef4f8; font-weight: 680; }
    .muted { color: #65727f; }
    .stack { display: grid; gap: 18px; }
    .panel { background: white; border: 1px solid #d9e1ea; padding: 14px; }
    .panel h2 { margin: 0 0 10px; font-size: 18px; letter-spacing: 0; }
    .task { border-top: 1px solid #e7edf3; padding: 10px 0; }
    .task:first-of-type { border-top: 0; }
    .task-actions { display: flex; gap: 8px; margin-top: 8px; }
    .task-actions button { padding: 7px 10px; font-size: 13px; }
    .metric { display: grid; grid-template-columns: 1fr auto; gap: 10px; padding: 7px 0; border-top: 1px solid #e7edf3; }
    .metric:first-of-type { border-top: 0; }
    .status { display: inline-block; padding: 2px 8px; border-radius: 6px; background: #e7f4ea; color: #137333; font-size: 12px; }
    .status.failed { background: #fce8e6; color: #a50e0e; }
    .status.running, .status.queued { background: #e8f0fe; color: #174ea6; }
    .status.warning { background: #fef7e0; color: #b06000; }
    .status.critical { background: #fce8e6; color: #a50e0e; }
    .status.partial, .status.degraded { background: #fef7e0; color: #b06000; }
    .status.repair_required { background: #fce8e6; color: #a50e0e; }
    .status.ready, .status.succeeded { background: #e7f4ea; color: #137333; }
    .status.enriching { background: #e8f0fe; color: #174ea6; }
    .actions { display: flex; gap: 8px; }
    .secondary { background: white; color: #1b6f79; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; padding: 14px; } header { padding: 18px 14px; } }
  </style>
</head>
<body>
  <header>
    <h1>CodeSpine Index Explorer</h1>
    <div class="muted">Local view of core readiness, repair state, and background work.</div>
  </header>
  <main>
    <section>
      <div class="toolbar">
        <input id="filter" placeholder="Filter projects by id or path">
        <button id="refresh">Refresh</button>
      </div>
      <table>
        <thead><tr><th>Project</th><th>State</th><th>Shard</th><th>Files</th><th>Classes</th><th>Methods</th><th>Calls</th><th>Path</th><th>Actions</th></tr></thead>
        <tbody id="projects"><tr><td colspan="9" class="muted">Loading...</td></tr></tbody>
      </table>
    </section>
    <aside class="stack">
      <section class="panel">
        <h2>Index Health</h2>
        <div id="health" class="muted">Loading...</div>
      </section>
      <section class="panel">
        <h2>Background Tasks</h2>
        <div id="tasks" class="muted">Loading...</div>
      </section>
      <section class="panel">
        <h2>Install</h2>
        <div class="muted">Use <code>codespine background</code> for the same task state in the terminal. Repair actions here call the same local CLI flows.</div>
      </section>
    </aside>
  </main>
  <script>
    let projects = [];
    async function getJSON(url) { const r = await fetch(url); if (!r.ok) throw new Error(url); return await r.json(); }
    async function postJSON(url, body) {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      if (!r.ok) throw new Error(await r.text());
      return await r.json();
    }
    function renderProjects() {
      const q = document.getElementById('filter').value.toLowerCase();
      const rows = projects.filter(p => (`${p.id || ''} ${p.path || ''}`).toLowerCase().includes(q));
      document.getElementById('projects').innerHTML = rows.length ? rows.map(p => `
        <tr>
          <td>${p.id || ''}</td>
          <td><span class="status ${p.project_state || ''}">${p.project_state || '-'}</span></td>
          <td>${p.shard}</td>
          <td>${p.files}</td>
          <td>${p.classes}</td>
          <td>${p.methods}</td>
          <td>${p.calls}</td>
          <td class="muted">${p.path || ''}${p.last_error ? `<div>${p.last_error}</div>` : ''}</td>
          <td>
            <div class="actions">
              <button class="secondary" onclick="repairProject('${p.id || p.path || ''}', 'auto')">Repair</button>
              <button onclick="repairProject('${p.id || p.path || ''}', 'full')">Reindex</button>
            </div>
          </td>
        </tr>
      `).join('') : '<tr><td colspan="9" class="muted">No projects found.</td></tr>';
    }
    function renderTasks(tasks) {
      const el = document.getElementById('tasks');
      if (!tasks.length) { el.innerHTML = 'No background tasks.'; return; }
      el.innerHTML = tasks.map(t => `
        <div class="task">
          <div><span class="status ${t.status}">${t.status}</span> <span class="status ${t.result_status || ''}">${t.result_status || 'pending'}</span> <strong>${t.last_phase || t.phase || t.kind}</strong></div>
          <div class="muted">${t.label || ''}</div>
          <div class="muted">${t.path || ''}</div>
          <div class="muted">Progress: ${typeof t.progress === 'number' ? Math.round(t.progress * 100) + '%' : '-'}</div>
          ${t.detail ? `<div>${t.detail}</div>` : ''}
          ${t.repair_hint ? `<div class="muted">Repair: ${t.repair_hint}</div>` : ''}
          ${t.status === 'failed' ? `<div class="task-actions"><button class="secondary" onclick="repairProject('${t.project_id || t.path || ''}', 'auto')">Repair</button><button onclick="repairProject('${t.project_id || t.path || ''}', 'full')">Reindex</button></div>` : ''}
        </div>
      `).join('');
    }
    function renderHealth(health) {
      const el = document.getElementById('health');
      const summary = health.summary || {};
      const projects = health.projects || [];
      const critical = summary.critical_count || 0;
      const anomalies = summary.anomaly_count || 0;
      const status = critical ? 'critical' : anomalies ? 'warning' : '';
      const worst = critical ? 'critical' : anomalies ? 'warning' : 'ok';
      const lowest = projects.reduce((min, p) => Math.min(min, Number(p.call_edge_coverage || 0)), projects.length ? 1 : 0);
      const coverage = projects.length ? `${Math.round(lowest * 1000) / 10}%` : '-';
      el.innerHTML = `
        <div><span class="status ${status}">${worst}</span></div>
        <div class="metric"><span>Projects</span><strong>${summary.project_count || 0}</strong></div>
        <div class="metric"><span>Anomalies</span><strong>${anomalies}</strong></div>
        <div class="metric"><span>Lowest call coverage</span><strong>${coverage}</strong></div>
        ${projects.flatMap(p => (p.anomalies || []).map(a => `<div class="task"><strong>${p.project_id}</strong><div>${a.message || ''}</div></div>`)).join('')}
      `;
    }
    async function repairProject(target, mode) {
      if (!target) return;
      const payload = await postJSON('/api/repair', { project_id: target, mode });
      alert(`Started ${payload.mode} repair\\nTask: ${payload.task_id}`);
      await refresh();
    }
    async function refresh() {
      const [p, t, h] = await Promise.all([getJSON('/api/projects'), getJSON('/api/tasks'), getJSON('/api/health')]);
      projects = p; renderProjects(); renderTasks(t); renderHealth(h);
    }
    document.getElementById('filter').addEventListener('input', renderProjects);
    document.getElementById('refresh').addEventListener('click', refresh);
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>"""


@main.command("ui")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, show_default=True, type=int)
@click.option("--open/--no-open", "open_browser", default=False, show_default=True)
def ui(host: str, port: int, open_browser: bool) -> None:
    """Serve a lightweight local read-only index explorer."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            LOGGER.debug("ui: " + fmt, *args)

        def _send(self, body: bytes, content_type: str, status_code: int = HTTPStatus.OK) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: object) -> None:
            self._send(json.dumps(data, indent=2).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(_ui_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/tasks":
                self._json(list_tasks(include_finished=True, limit=30))
                return
            if parsed.path == "/api/projects":
                self._json(_project_summaries())
                return
            if parsed.path == "/api/health":
                self._json(index_health(_open_store(read_only=True)))
                return
            if parsed.path == "/api/status":
                self._json({
                    "tasks": list_tasks(include_finished=True, limit=30),
                    "projects": _project_summaries(),
                    "health": index_health(_open_store(read_only=True)),
                })
                return
            self._send(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != "/api/repair":
                self._send(b"not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0") or 0)
                raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                body = json.loads(raw_body.decode("utf-8") or "{}")
                target = str(body.get("project_id") or body.get("path") or "").strip()
                mode = str(body.get("mode") or "auto").strip().lower()
                if not target:
                    raise click.ClickException("repair target is required")
                self._json(_start_repair(target, force_full=(mode == "full")))
                return
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)[:300]})

    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    click.secho(f"CodeSpine UI running at {url}", fg="green")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopping UI.")
    finally:
        server.server_close()


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
    try:
        overlay = get_overlay_status(store)
    except Exception:
        overlay = []
    tasks = active_tasks(limit=10)
    project_rows = _project_summaries()
    try:
        health_summary = index_health(store).get("summary", {})
    except Exception:
        health_summary = {}

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
        "background_tasks": tasks,
        "projects": project_rows,
        "health_summary": health_summary,
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
        if tasks:
            click.echo("\nBackground tasks:")
            for task in tasks:
                click.echo(
                    f"  {task.get('id')}  {task.get('status')}  "
                    f"{task.get('last_phase') or task.get('phase')}  {task.get('path') or ''}"
                )
        if project_rows:
            click.echo("\nProjects:")
            for project in project_rows[:10]:
                click.echo(
                    f"  {project.get('id')}  {project.get('project_state')}  "
                    f"{project.get('path') or ''}"
                )
        if health_summary:
            click.echo(
                "\nIndex health: "
                f"{health_summary.get('project_count', 0)} project(s), "
                f"{health_summary.get('anomaly_count', 0)} anomaly(s), "
                f"{health_summary.get('critical_count', 0)} critical"
            )


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
    click.echo("  pip install -e '.[ui]'                  # core + local browser explorer")
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
