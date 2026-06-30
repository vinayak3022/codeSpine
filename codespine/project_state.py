from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any
from urllib.parse import quote, unquote

from codespine.config import SETTINGS
from codespine.sharding.router import ShardRouter

_DEFAULT_CORE_STATE = "repair_required"
_DEFAULT_DEEP_STATE = "idle"


def _now() -> float:
    return time.time()


def _state_dir() -> str:
    base = os.path.join(SETTINGS.index_meta_dir, "project_state")
    os.makedirs(base, exist_ok=True)
    return base


def _state_file_name(project_id: str) -> str:
    return f"{quote(project_id, safe='')}.json"


def _state_path(project_id: str) -> str:
    return os.path.join(_state_dir(), _state_file_name(project_id))


def _default_state(project_id: str) -> dict[str, Any]:
    now = _now()
    return {
        "project_id": project_id,
        "path": "",
        "core_state": _DEFAULT_CORE_STATE,
        "deep_state": _DEFAULT_DEEP_STATE,
        "last_error": "",
        "last_task_id": None,
        "last_good_snapshot_at": None,
        "repair_hint": "",
        "dependency_project_ids": [],
        "declared_dependencies": [],
        "unresolved_dependency_coords": [],
        "maven_coord": None,
        "maven_group_id": None,
        "maven_artifact_id": None,
        "maven_version": None,
        "maven_packaging": None,
        "updated_at": now,
    }


def synthetic_project_state(
    project_id: str,
    *,
    path: str = "",
    core_state: str = "ready",
    deep_state: str = "idle",
) -> dict[str, Any]:
    state = _default_state(project_id)
    state.update(
        {
            "path": path,
            "core_state": core_state,
            "deep_state": deep_state,
            "last_error": "",
            "repair_hint": "",
        }
    )
    return state


def _coerce_state(data: dict[str, Any], project_id: str) -> dict[str, Any]:
    state = _default_state(project_id)
    state.update({k: v for k, v in data.items() if v is not None})
    state["project_id"] = project_id
    if state.get("path"):
        state["path"] = os.path.abspath(str(state["path"]))
    state["core_state"] = str(state.get("core_state") or _DEFAULT_CORE_STATE)
    state["deep_state"] = str(state.get("deep_state") or _DEFAULT_DEEP_STATE)
    if "updated_at" not in state or state["updated_at"] is None:
        state["updated_at"] = _now()
    return state


def load_project_state(project_id: str) -> dict[str, Any]:
    path = _state_path(project_id)
    if not os.path.exists(path):
        return _default_state(project_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return _coerce_state(data, project_id)
    except Exception:
        pass
    return _default_state(project_id)


def save_project_state(state: dict[str, Any]) -> dict[str, Any]:
    project_id = str(state.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("project_state requires project_id")
    merged = _coerce_state(state, project_id)
    parent = _state_dir()
    fd, tmp_path = tempfile.mkstemp(prefix=".codespine_project_state_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, _state_path(project_id))
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return merged


def update_project_state(project_id: str, **fields: Any) -> dict[str, Any]:
    state = load_project_state(project_id)
    for key, value in fields.items():
        if value is None and key not in {"last_task_id", "last_good_snapshot_at"}:
            continue
        state[key] = value
    state["updated_at"] = _now()
    return save_project_state(state)


def list_project_states() -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    base = _state_dir()
    for name in sorted(os.listdir(base)):
        if not name.endswith(".json"):
            continue
        project_id = unquote(name[:-5])
        states.append(load_project_state(project_id))
    states.sort(key=lambda item: str(item.get("project_id") or ""))
    return states


def delete_project_state(project_id: str) -> None:
    path = _state_path(project_id)
    if not os.path.exists(path):
        return
    try:
        os.remove(path)
    except OSError:
        pass


def record_snapshot_success(project_id: str, when: float | None = None) -> dict[str, Any]:
    return update_project_state(
        project_id,
        last_good_snapshot_at=when or _now(),
        last_error="",
        repair_hint="",
    )


def snapshot_info(project_id: str, router: ShardRouter | None = None) -> dict[str, Any]:
    from codespine.db.duckdb_store import is_valid_duckdb_database_path

    fallback_router = ShardRouter()
    active_router = router if router is not None and hasattr(router, "shard_for") else fallback_router
    shard = active_router.shard_for(project_id)
    if router is not None and (not hasattr(router, "snapshot_path") or not hasattr(router, "db_path")):
        return {
            "shard": shard,
            "snapshot_path": "",
            "snapshot_exists": False,
            "snapshot_valid": True,
            "write_db_path": "",
            "write_db_exists": False,
            "write_db_valid": True,
            "active_read_path": None,
        }
    path_router = router if router is not None and hasattr(router, "snapshot_path") and hasattr(router, "db_path") else fallback_router
    snapshot_path = path_router.snapshot_path(shard)
    db_path = path_router.db_path(shard)
    snapshot_exists = os.path.exists(snapshot_path)
    snapshot_valid = is_valid_duckdb_database_path(snapshot_path)
    write_db_exists = os.path.exists(db_path)
    write_db_valid = is_valid_duckdb_database_path(db_path)
    active_read_path = snapshot_path if snapshot_valid else (db_path if write_db_valid else None)
    return {
        "shard": shard,
        "snapshot_path": snapshot_path,
        "snapshot_exists": snapshot_exists,
        "snapshot_valid": snapshot_valid,
        "write_db_path": db_path,
        "write_db_exists": write_db_exists,
        "write_db_valid": write_db_valid,
        "active_read_path": active_read_path,
    }


def derive_project_status(
    state: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
) -> str:
    core_state = str(state.get("core_state") or _DEFAULT_CORE_STATE)
    deep_state = str(state.get("deep_state") or _DEFAULT_DEEP_STATE)
    if snapshot is not None and not snapshot.get("snapshot_valid") and not snapshot.get("write_db_valid"):
        return "repair_required"
    if core_state == "repair_required":
        return "repair_required"
    if core_state == "partial":
        return "partial"
    if core_state == "indexing":
        return "enriching" if state.get("last_good_snapshot_at") else "repair_required"
    if deep_state in {"queued", "running"}:
        return "enriching"
    if deep_state == "failed":
        return "degraded"
    return "ready"


def repair_hint_for(
    project_id: str | None = None,
    path: str | None = None,
    *,
    full: bool = False,
) -> str:
    target = path or project_id or "<project>"
    if " " in target:
        target = f"\"{target}\""
    return f"codespine repair {'--full ' if full else ''}{target}".strip()


def project_dependency_graph(store=None, project: str | None = None, *, reverse: bool = False) -> dict:
    """Return the project dependency graph as {nodes: [...], edges: [...]}.

    When *project* is given, only returns the subgraph reachable from that
    project.  When *reverse* is True, returns the reverse dependency graph
    (projects that depend on the given project).
    """
    states = list_project_states()
    node_map: dict[str, dict] = {}
    edges: list[dict] = []
    for st in states:
        pid = st.get("project_id")
        if not pid:
            continue
        node_map[pid] = {
            "id": pid,
            "project_id": pid,
            "name": pid,
            "path": st.get("path", ""),
            "indexed_at": st.get("last_good_snapshot_at"),
        }
        dep_ids: list[str] = st.get("dependency_project_ids") or []
        for dep_id in dep_ids:
            if dep_id == pid:
                continue
            edges.append({
                "src": pid if not reverse else dep_id,
                "dst": dep_id if not reverse else pid,
                "direction": "reverse" if reverse else "forward",
            })
    nodes = sorted(node_map.values(), key=lambda n: n["id"])
    if project:
        adj: dict[str, list[str]] = {n["id"]: [] for n in nodes}
        for e in edges:
            adj.setdefault(e["src"], []).append(e["dst"])
        reachable: set[str] = set()
        queue = [project]
        while queue:
            pid = queue.pop(0)
            if pid in reachable:
                continue
            reachable.add(pid)
            for neighbor in adj.get(pid, []):
                if neighbor not in reachable:
                    queue.append(neighbor)
        nodes = [n for n in nodes if n["id"] in reachable]
        edges = [e for e in edges if e["src"] in reachable and e["dst"] in reachable]
    return {"nodes": nodes, "edges": edges}


def project_dependency_closure(project: str | None, *, include_self: bool = True) -> list[str]:
    """Return the transitive dependency closure for *project*.

    Returns a list of project IDs that *project* depends on (directly or
    transitively).  When *include_self* is True (default), the list includes
    *project* itself.  Returns an empty list when *project* is None.
    """
    if not project:
        return []
    visited: set[str] = set()
    queue = [project]
    while queue:
        pid = queue.pop(0)
        if pid in visited:
            continue
        visited.add(pid)
        state = load_project_state(pid)
        dep_ids: list[str] = state.get("dependency_project_ids") or []
        for dep_id in dep_ids:
            if dep_id not in visited:
                queue.append(dep_id)
    result = list(visited)
    if not include_self:
        result = [pid for pid in result if pid != project]
    return result
