from __future__ import annotations

import os
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

    abs_path = os.path.abspath(path)
    root_basename = os.path.basename(abs_path)
    indexer = JavaIndexer(store)

    # Detect module structure at startup
    module_dirs = JavaIndexer.detect_modules(abs_path)
    is_multi = not (len(module_dirs) == 1 and module_dirs[0] == abs_path)

    if is_multi:
        module_map: dict[str, str] = {
            m: f"{root_basename}::{os.path.basename(m)}" for m in module_dirs
        }
        print(
            f"Watching {abs_path} ({len(module_map)} modules): "
            + ", ".join(os.path.basename(m) for m in module_dirs)
        )
    else:
        module_map = {abs_path: root_basename}
        print(f"Watching {abs_path} for changes...")

    # Sort module paths longest-first for prefix matching
    sorted_modules = sorted(module_map.keys(), key=len, reverse=True)

    last_global = 0.0

    for changes in watch(abs_path):
        changed_java = [p for _, p in changes if p.endswith(".java")]
        if not changed_java:
            continue

        # Determine which modules were affected
        affected: set[str] = set()
        for file_path in changed_java:
            for m_path in sorted_modules:
                if file_path.startswith(m_path + os.sep) or file_path == m_path:
                    affected.add(m_path)
                    break
            else:
                # File doesn't match any known module – re-index root
                affected.add(abs_path)

        for m_path in sorted(affected):
            pid = module_map.get(m_path, root_basename)
            start = time.time()
            indexer.index_project(m_path, full=False, project_id=pid)
            elapsed = time.time() - start
            label = pid if is_multi else abs_path
            print(f"[{time.strftime('%H:%M:%S')}] {label}: {len(changed_java)} file(s) modified -> re-indexed ({elapsed:.1f}s)")

        if time.time() - last_global >= global_interval:
            detect_communities(store)
            detect_dead_code(store, limit=200)
            trace_execution_flows(store)
            # Coupling computed against the watched root and grouped under root project
            compute_coupling(store, abs_path, root_basename)
            last_global = time.time()
