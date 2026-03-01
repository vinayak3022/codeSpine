from __future__ import annotations

import time

from codespine.analysis.community import detect_communities
from codespine.analysis.coupling import compute_coupling
from codespine.analysis.deadcode import detect_dead_code
from codespine.analysis.flow import trace_execution_flows
from codespine.config import SETTINGS
from codespine.indexer.engine import JavaIndexer


def run_watch_mode(store, path: str, global_interval: int = SETTINGS.default_global_interval_s) -> None:
    try:
        from watchfiles import watch
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("watchfiles is required for watch mode") from exc

    indexer = JavaIndexer(store)
    last_global = 0.0
    print(f"Watching {path} for changes...")

    for changes in watch(path):
        changed_java = [p for _, p in changes if p.endswith(".java")]
        if not changed_java:
            continue

        start = time.time()
        result = indexer.index_project(path, full=False)
        elapsed = time.time() - start
        print(f"[{time.strftime('%H:%M:%S')}] {len(changed_java)} file(s) modified -> re-indexed ({elapsed:.1f}s)")

        if time.time() - last_global >= global_interval:
            detect_communities(store)
            detect_dead_code(store, limit=200)
            trace_execution_flows(store)
            compute_coupling(store, path, result.project_id)
            last_global = time.time()
