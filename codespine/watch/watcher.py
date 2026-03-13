from __future__ import annotations

import os
import threading
import time

from codespine.analysis.community import detect_communities
from codespine.analysis.coupling import compute_coupling
from codespine.analysis.deadcode import detect_dead_code
from codespine.analysis.flow import trace_execution_flows
from codespine.config import SETTINGS
from codespine.indexer.engine import JavaIndexer
from codespine.overlay.git_state import current_head, git_repo_root
from codespine.overlay.store import OverlayStore, build_overlay_file_entry


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
    overlay_store: OverlayStore = store.overlay_store
    statuses = overlay_store.status(project)
    out: list[dict] = []
    for item in statuses:
        metadata = store.get_project_metadata(item["project_id"]) or {}
        out.append(
            {
                **item,
                "indexed_commit": metadata.get("indexed_commit", ""),
                "overlay_dirty": bool(metadata.get("overlay_dirty", False)),
                "indexed_at": metadata.get("indexed_at", ""),
                "promotion_pending": bool(
                    item.get("overlay_present")
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
        meta = store.get_project_metadata(project_id)
        if meta:
            store.set_project_overlay_dirty(project_id, False)
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


def _update_overlay_for_files(store, project_path: str, project_id: str, file_paths: list[str]) -> dict:
    overlay_store: OverlayStore = store.overlay_store
    indexer = JavaIndexer(store)
    metadata = store.get_project_metadata(project_id) or {}
    repo_root = git_repo_root(project_path)
    indexed_commit = str(metadata.get("indexed_commit") or "")
    head = current_head(project_path)
    existing_doc = overlay_store.load_project(project_id)

    base_method_catalog = indexer._existing_method_catalog(project_id)
    base_class_catalog = indexer._existing_class_catalog(project_id)
    base_class_ids = indexer._existing_class_ids_by_fqcn(project_id)
    base_class_methods = indexer._existing_class_methods(project_id)
    embed = store.project_has_embeddings(project_id)

    changed = deleted = 0
    for file_path in sorted(set(os.path.abspath(p) for p in file_paths)):
        if not file_path.endswith(".java"):
            continue
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
                existing_overlay_doc=existing_doc,
            )
            overlay_store.upsert_file(
                project_id=project_id,
                project_path=project_path,
                repo_root=repo_root,
                base_commit=indexed_commit,
                current_head=head,
                file_path=file_path,
                entry=entry,
            )
            existing_doc = overlay_store.load_project(project_id)
            changed += 1
        else:
            overlay_store.mark_deleted(
                project_id=project_id,
                project_path=project_path,
                repo_root=repo_root,
                base_commit=indexed_commit,
                current_head=head,
                file_path=file_path,
            )
            existing_doc = overlay_store.load_project(project_id)
            deleted += 1
    if changed or deleted:
        if metadata:
            store.set_project_overlay_dirty(project_id, True)
        else:
            store.upsert_project(project_id, project_path)
            store.set_project_indexed_commit(project_id, indexed_commit)
            store.set_project_overlay_dirty(project_id, True)
    return {"project_id": project_id, "changed": changed, "deleted": deleted}


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

    def _head_monitor() -> None:
        while not stop_event.wait(max(1, global_interval)):
            head = current_head(abs_path)
            if not head:
                continue
            if head_state["value"] and head != head_state["value"] and promote_on_commit:
                promoted = promote_overlay(store, require_head_change=True)
                for item in promoted:
                    print(
                        f"[{time.strftime('%H:%M:%S')}] {item['project_id']}: promoted overlay at {item.get('head') or 'unknown'}"
                    )
            head_state["value"] = head

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
                result = _update_overlay_for_files(store, module_path, project_id, files)
                elapsed = time.time() - start
                print(
                    f"[{time.strftime('%H:%M:%S')}] {project_id}: overlay updated "
                    f"({result['changed']} changed, {result['deleted']} deleted) in {elapsed:.1f}s"
                )

            if promote_on_commit:
                head = current_head(repo_root)
                if head and head_state["value"] and head != head_state["value"]:
                    promoted = promote_overlay(store, require_head_change=True)
                    for item in promoted:
                        print(
                            f"[{time.strftime('%H:%M:%S')}] {item['project_id']}: promoted overlay at {item.get('head') or 'unknown'}"
                        )
                    head_state["value"] = head
    finally:
        stop_event.set()
        monitor.join(timeout=1)


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
