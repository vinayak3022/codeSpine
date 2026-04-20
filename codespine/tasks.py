from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from typing import Any

import psutil

from codespine.config import SETTINGS

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}


def _now() -> float:
    return time.time()


def _registry_path() -> str:
    return SETTINGS.task_registry_path


def _load_raw() -> dict[str, Any]:
    path = _registry_path()
    if not os.path.exists(path):
        return {"tasks": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            return data
    except Exception:
        pass
    return {"tasks": []}


def _save_raw(data: dict[str, Any]) -> None:
    path = _registry_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".codespine_tasks_", suffix=".json", dir=parent or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _refresh_liveness(task: dict[str, Any], now: float) -> dict[str, Any]:
    if task.get("status") not in {"queued", "running"}:
        return task
    pid = task.get("pid")
    if not pid:
        return task
    try:
        alive = psutil.pid_exists(int(pid))
    except Exception:
        alive = False
    if not alive:
        task = dict(task)
        task["status"] = "failed"
        task["phase"] = "lost"
        task["detail"] = "Process is no longer running and did not mark completion."
        task["finished_at"] = now
        task["updated_at"] = now
    return task


def create_task(kind: str, label: str, path: str | None = None, metadata: dict[str, Any] | None = None) -> str:
    data = _load_raw()
    now = _now()
    task_id = uuid.uuid4().hex[:12]
    data["tasks"].append(
        {
            "id": task_id,
            "kind": kind,
            "label": label,
            "path": path,
            "status": "queued",
            "phase": "queued",
            "detail": "",
            "progress": None,
            "pid": None,
            "metadata": metadata or {},
            "started_at": now,
            "updated_at": now,
            "finished_at": None,
        }
    )
    _save_raw(data)
    return task_id


def update_task(task_id: str, **fields: Any) -> None:
    data = _load_raw()
    now = _now()
    for task in data["tasks"]:
        if task.get("id") == task_id:
            task.update(fields)
            task["updated_at"] = now
            break
    _save_raw(data)


def finish_task(task_id: str, status: str = "succeeded", detail: str | None = None) -> None:
    fields: dict[str, Any] = {
        "status": status,
        "phase": status,
        "finished_at": _now(),
    }
    if detail is not None:
        fields["detail"] = detail
    update_task(task_id, **fields)


def list_tasks(include_finished: bool = True, limit: int = 20) -> list[dict[str, Any]]:
    data = _load_raw()
    now = _now()
    refreshed = [_refresh_liveness(dict(task), now) for task in data["tasks"]]
    if refreshed != data["tasks"]:
        _save_raw({"tasks": refreshed})
    tasks = refreshed if include_finished else [t for t in refreshed if t.get("status") not in TERMINAL_STATUSES]
    tasks.sort(key=lambda t: float(t.get("updated_at") or t.get("started_at") or 0), reverse=True)
    return tasks[:limit]


def active_tasks(limit: int = 20) -> list[dict[str, Any]]:
    return list_tasks(include_finished=False, limit=limit)

