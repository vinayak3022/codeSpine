from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import traceback

from codespine.analysis.community import detect_communities
from codespine.analysis.coupling import compute_coupling
from codespine.analysis.deadcode import detect_dead_code
from codespine.analysis.flow import trace_execution_flows
from codespine.config import SETTINGS
from codespine.indexer.engine import JavaIndexer
from codespine.overlay.git_state import current_head, git_repo_root
from codespine.overlay.store import OverlayStore, build_overlay_file_entry

LOGGER = logging.getLogger(__name__)

# Reduced from 30 s → 5 s so commits are picked up quickly.
_HEAD_POLL_INTERVAL_S = 5


def _project_modules(root_path: str) -> tuple[dict[str, str], list[str], bool]:
    abs_path = os.path.abspath(root_path)
    root_basename = os.path.basename(abs_path)
    module_dirs = JavaIndexer.detect_modules(abs_path)
    is_multi = not (len(module_dirs) == 1 and module_dirs[0] == abs_path)
    if is_multi:
        module_map = {m: f"{root_basename}::{os.path.basename(m)}" for m in module_dirs}
    else:
        module_map = {abs_path: root_basename}
    sorted_modules = sorted(module_map.keys(), key=len, reverse=True)
    return module_map, sorted_modules, is_multi


def _module_for_file(file_path: str, sorted_modules: list[str], default_root: str) -> str:
    for module_path in sorted_modules:
        if file_path.startswith(module_path + os.sep) or file_path == module_path:
            return module_path
    return default_root


def get_overlay_status(store, project: str | None = None) -> list[dict]:
    overlay_store: OverlayStore | None = getattr(store, "overlay_store", None)
    if overlay_store is None:
        return []
    statuses = overlay_store.status(project)
    out: list[dict] = []
    for item in statuses:
        try:
            metadata = store.get_project_metadata(item["project_id"]) or {}
        except Exception:
            metadata = {}
        # The overlay JSON on disk is the source of truth; the DB flag
        # may be stale if the watch process couldn't write to the DB.
        overlay_present = bool(item.get("overlay_present"))
        db_dirty = bool(metadata.get("overlay_dirty", False))
        out.append(
            {
                **item,
                "indexed_commit": metadata.get("indexed_commit", ""),
                "overlay_dirty": overlay_present or db_dirty,
                "indexed_at": metadata.get("indexed_at", ""),
                "promotion_pending": bool(
                    overlay_present
                    and item.get("current_head")
                    and metadata.get("indexed_commit")
                    and item.get("current_head") != metadata.get("indexed_commit")
                ),
            }
        )
    return out


def clear_overlay(store, project: str | None = None) -> list[str]:
    overlay_store: OverlayStore = store.overlay_store
    targets = [project] if project else [item["project_id"] for item in overlay_store.status()]
    cleared: list[str] = []
    for project_id in targets:
        overlay_store.clear_project(project_id)
        try:
            meta = store.get_project_metadata(project_id)
            if meta:
                store.set_project_overlay_dirty(project_id, False)
        except Exception as exc:
            LOGGER.warning("clear_overlay: could not update DB dirty flag for %s (%s); overlay files cleared", project_id, exc)
        cleared.append(project_id)
    return cleared


def promote_overlay(store, project: str | None = None, require_head_change: bool = False) -> list[dict]:
    overlay_store: OverlayStore = store.overlay_store
    indexer = JavaIndexer(store)
    targets = [project] if project else [item["project_id"] for item in overlay_store.status()]
    promoted: list[dict] = []
    for project_id in targets:
        doc = overlay_store.load_project(project_id)
        if not doc.get("dirty_files") and not doc.get("deleted_files"):
            continue
        project_path = doc.get("project_path") or ""
        if not project_path:
            continue
        metadata = store.get_project_metadata(project_id) or {}
        head = current_head(project_path)
        indexed_commit = str(metadata.get("indexed_commit") or "")
        if require_head_change and head and indexed_commit and head == indexed_commit:
            continue
        embed = store.project_has_embeddings(project_id)
        result = indexer.index_project(project_path, full=False, project_id=project_id, embed=embed)
        if head:
            store.set_project_indexed_commit(project_id, head)
        store.set_project_overlay_dirty(project_id, False)
        overlay_store.clear_project(project_id)
        promoted.append(
            {
                "project_id": project_id,
                "head": head,
                "files_indexed": result.files_indexed,
                "calls_resolved": result.calls_resolved,
            }
        )
    return promoted


def _update_files_in_graph(store, project_path: str, project_id: str, file_paths: list[str]) -> dict:
    """Write changed Java files directly to the graph DB (no overlay JSON).

    This replaces the old overlay-on-save path.  Changes are immediately
    visible to the MCP server after snapshot_to_read_replica() is called.

    For deleted files the file's nodes/edges are removed from the graph.
    For changed/new files the full entry is re-parsed and upserted atomically.
    """
    indexer = JavaIndexer(store)

    def _refresh_batch_catalogs() -> None:
        nonlocal base_method_catalog, base_class_catalog, base_class_ids, base_class_methods
        try:
            base_method_catalog = indexer._existing_method_catalog(project_id)
            base_class_catalog = indexer._existing_class_catalog(project_id)
            base_class_ids = indexer._existing_class_ids_by_fqcn(project_id)
            base_class_methods = indexer._existing_class_methods(project_id)
        except Exception as exc:
            LOGGER.warning("watch: DB read failed for catalogs (%s), using empty", exc)
            base_method_catalog = {}
            base_class_catalog = {}
            base_class_ids = {}
            base_class_methods = {}

    # Load catalogs once so call-resolution works across files in this batch.
    base_method_catalog: dict[str, dict] = {}
    base_class_catalog: dict[str, list[str]] = {}
    base_class_ids: dict[str, list[str]] = {}
    base_class_methods: dict[str, dict[str, str]] = {}
    _refresh_batch_catalogs()

    try:
        embed = store.project_has_embeddings(project_id)
    except Exception:
        embed = False

    # Ensure the project node exists so upsert_file_from_entry can reference it.
    try:
        meta = store.get_project_metadata(project_id)
        if not meta:
            store.upsert_project(project_id, project_path)
    except Exception as exc:
        LOGGER.warning("watch: could not ensure project node (%s)", exc)

    # Build a running overlay-like catalog so that inter-file call resolution
    # works when multiple files are changed in the same batch.
    running_overlay_doc: dict = {
        "project_id": project_id,
        "project_path": project_path,
        "dirty_files": {},
        "deleted_files": [],
    }

    changed = deleted = errors = 0
    for file_path in sorted(set(os.path.abspath(p) for p in file_paths)):
        if not file_path.endswith(".java"):
            continue
        try:
            if os.path.exists(file_path):
                with open(file_path, "rb") as fh:
                    source = fh.read()
                entry = build_overlay_file_entry(
                    store=store,
                    project_id=project_id,
                    project_path=project_path,
                    file_path=file_path,
                    source=source,
                    embed=embed,
                    base_method_catalog=base_method_catalog,
                    base_class_catalog=base_class_catalog,
                    base_class_ids_by_fqcn=base_class_ids,
                    base_class_methods=base_class_methods,
                    existing_overlay_doc=running_overlay_doc,
                )
                # Write directly to graph DB — no overlay JSON.
                store.upsert_file_from_entry(entry, project_path)
                # Update running catalog for subsequent files in same batch.
                running_overlay_doc["dirty_files"][os.path.abspath(file_path)] = entry
                changed += 1
            else:
                # File deleted: remove its nodes/edges from the graph.
                store.clear_file_by_path(project_id, project_path, file_path)
                # Refresh the batch-local catalogs so later files do not resolve
                # against symbols that were just removed.
                _refresh_batch_catalogs()
                deleted += 1
        except Exception as exc:
            LOGGER.warning("watch: failed to process %s: %s", file_path, exc)
            errors += 1

    return {"project_id": project_id, "changed": changed, "deleted": deleted, "errors": errors}


def _git_changed_files(repo_root: str, old_head: str, new_head: str) -> list[str] | None:
    """Return absolute paths of Java files changed between two commits."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", old_head, new_head],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            LOGGER.warning("watch: git diff failed: %s", result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}")
            return None
        files = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.endswith(".java"):
                abs_path = os.path.join(repo_root, line)
                files.append(abs_path)
        return files
    except Exception as exc:
        LOGGER.warning("watch: git diff failed: %s", exc)
        return None


def _reindex_commit_change(
    store,
    abs_path: str,
    repo_root: str,
    module_map: dict[str, str],
    sorted_modules: list[str],
    head_state: dict[str, str | None],
    new_head: str,
    promote_on_commit: bool,
) -> bool:
    """Re-index a commit change and advance stored head only on success."""
    old_head = head_state["value"]
    if not old_head or new_head == old_head:
        head_state["value"] = new_head
        return True

    if not promote_on_commit:
        head_state["value"] = new_head
        return True

    success = True
    changed_files = _git_changed_files(repo_root, old_head, new_head)
    if changed_files is None:
        return False
    if changed_files:
        grouped: dict[str, list[str]] = {}
        for fp in changed_files:
            module_path = _module_for_file(fp, sorted_modules, abs_path)
            grouped.setdefault(module_path, []).append(fp)
        for module_path, files in sorted(grouped.items()):
            project_id = module_map.get(module_path, os.path.basename(module_path))
            start = time.time()
            try:
                result = _update_files_in_graph(store, module_path, project_id, files)
            except Exception as exc:
                LOGGER.error("watch: commit re-index failed for %s: %s", project_id, exc)
                print(f"[{time.strftime('%H:%M:%S')}] {project_id}: ERROR on commit re-index — {exc}")
                success = False
                continue
            elapsed = time.time() - start
            print(
                f"[{time.strftime('%H:%M:%S')}] {project_id}: commit re-index "
                f"({result['changed']} changed, {result['deleted']} deleted) "
                f"@ {new_head[:8]} in {elapsed:.1f}s"
            )
            if int(result.get("errors", 0)) > 0:
                success = False
                LOGGER.warning(
                    "watch: commit re-index for %s completed with %s file errors",
                    project_id,
                    result.get("errors"),
                )
        if success:
            for module_path, files in sorted(grouped.items()):
                project_id = module_map.get(module_path, os.path.basename(module_path))
                try:
                    store.set_project_indexed_commit(project_id, new_head)
                except Exception:
                    pass
            # Snapshot once after processing all modules for this commit.
            # Run in background so re-index latency isn't blocked by the copy.
            try:
                store.snapshot_to_read_replica(background=True)
            except Exception as exc:
                LOGGER.warning("watch: snapshot after commit failed: %s", exc)
    else:
        # No Java files changed — just update the indexed commit.
        for module_path, project_id in module_map.items():
            try:
                store.set_project_indexed_commit(project_id, new_head)
            except Exception:
                pass
        try:
            store.snapshot_to_read_replica(background=True)
        except Exception as exc:
            LOGGER.warning("watch: snapshot after commit failed: %s", exc)
        print(f"[{time.strftime('%H:%M:%S')}] commit {new_head[:8]}: no Java changes")

    if success:
        head_state["value"] = new_head
    return success


# Keep the old name as a shim so existing call-sites (e.g. tests) don't break.
def _update_overlay_for_files(store, project_path: str, project_id: str, file_paths: list[str]) -> dict:
    return _update_files_in_graph(store, project_path, project_id, file_paths)


def run_watch_mode(
    store,
    path: str,
    global_interval: int = SETTINGS.default_global_interval_s,
    overlay_debounce_ms: int = SETTINGS.default_overlay_debounce_ms,
    promote_on_commit: bool = True,
) -> None:
    try:
        from watchfiles import watch
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("watchfiles is required for watch mode") from exc

    abs_path = os.path.abspath(path)
    module_map, sorted_modules, is_multi = _project_modules(abs_path)
    if is_multi:
        print(
            f"Watching {abs_path} ({len(module_map)} modules): "
            + ", ".join(os.path.basename(m) for m in module_map)
        )
    else:
        print(f"Watching {abs_path} for changes...")

    repo_root = git_repo_root(abs_path) or abs_path
    head_state = {"value": current_head(abs_path)}
    stop_event = threading.Event()

    # Use _HEAD_POLL_INTERVAL_S (5 s) rather than global_interval (30 s) so
    # commits are detected quickly.  global_interval is kept for compatibility
    # but is no longer the commit-poll cadence.
    _poll_interval = min(_HEAD_POLL_INTERVAL_S, max(1, global_interval))

    def _head_monitor() -> None:
        while not stop_event.wait(_poll_interval):
            try:
                head = current_head(abs_path)
                if not head:
                    continue
                old_head = head_state["value"]
                if old_head and head != old_head:
                    # Keep the stored head at the previous value until re-index succeeds,
                    # so a failed commit re-index is retried on the next poll.
                    if not _reindex_commit_change(
                        store,
                        abs_path,
                        repo_root,
                        module_map,
                        sorted_modules,
                        head_state,
                        head,
                        promote_on_commit,
                    ):
                        continue
                else:
                    head_state["value"] = head
            except Exception as exc:
                LOGGER.warning("watch: head monitor error: %s", exc)

    monitor = threading.Thread(target=_head_monitor, daemon=True)
    monitor.start()

    debounce_ms = max(100, int(overlay_debounce_ms))
    try:
        for changes in watch(abs_path, debounce=debounce_ms, step=max(50, debounce_ms // 3)):
            changed_java = [os.path.abspath(p) for _, p in changes if p.endswith(".java")]
            if not changed_java:
                continue
            grouped: dict[str, list[str]] = {}
            for file_path in changed_java:
                module_path = _module_for_file(file_path, sorted_modules, abs_path)
                grouped.setdefault(module_path, []).append(file_path)

            for module_path, files in sorted(grouped.items()):
                project_id = module_map.get(module_path, os.path.basename(module_path))
                start = time.time()
                try:
                    result = _update_files_in_graph(store, module_path, project_id, files)
                except Exception as exc:
                    LOGGER.error("watch: re-index failed for %s: %s\n%s", project_id, exc, traceback.format_exc())
                    print(f"[{time.strftime('%H:%M:%S')}] {project_id}: ERROR re-indexing — {exc}")
                    continue
                elapsed = time.time() - start
                err_note = f", {result.get('errors', 0)} errors" if result.get("errors") else ""
                print(
                    f"[{time.strftime('%H:%M:%S')}] {project_id}: re-indexed "
                    f"({result['changed']} changed, {result['deleted']} deleted{err_note}) in {elapsed:.1f}s"
                )

            # Snapshot to read replica after each file-change batch so the MCP
            # server picks up the new data without restarting.  Run in background
            # so rapid file saves don't block re-index latency.
            try:
                store.snapshot_to_read_replica(background=True)
            except Exception as exc:
                LOGGER.warning("watch: snapshot failed: %s", exc)
    finally:
        stop_event.set()
        monitor.join(timeout=2)


def run_deep_refresh(store, root_path: str, project_id: str) -> dict:
    communities = detect_communities(store)
    dead = detect_dead_code(store, limit=200)
    flows = trace_execution_flows(store)
    coupling = compute_coupling(store, root_path, project_id)
    return {
        "communities": len(communities),
        "dead_code": len(dead or []),
        "flows": len(flows),
        "coupling_pairs": len(coupling),
    }
