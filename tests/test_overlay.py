from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("kuzu")
pytest.importorskip("tree_sitter_java")

from codespine.config import SETTINGS
from codespine.db.store import GraphStore
from codespine.indexer.engine import JavaIndexer
from codespine.overlay.store import build_overlay_file_entry
from codespine.search.hybrid import hybrid_search
from codespine.analysis.impact import analyze_impact
from codespine.watch.watcher import get_overlay_status


def _write_java(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def isolated_settings(tmp_path: Path):
    original = {
        "db_path": SETTINGS.db_path,
        "overlay_dir": SETTINGS.overlay_dir,
        "index_meta_dir": SETTINGS.index_meta_dir,
        "embedding_cache_path": SETTINGS.embedding_cache_path,
        "pid_file": SETTINGS.pid_file,
        "log_file": SETTINGS.log_file,
    }
    object.__setattr__(SETTINGS, "db_path", str(tmp_path / "db"))
    object.__setattr__(SETTINGS, "overlay_dir", str(tmp_path / "overlay"))
    object.__setattr__(SETTINGS, "index_meta_dir", str(tmp_path / "meta"))
    object.__setattr__(SETTINGS, "embedding_cache_path", str(tmp_path / "embed.json"))
    object.__setattr__(SETTINGS, "pid_file", str(tmp_path / "codespine.pid"))
    object.__setattr__(SETTINGS, "log_file", str(tmp_path / "codespine.log"))
    try:
        yield
    finally:
        for key, value in original.items():
            object.__setattr__(SETTINGS, key, value)


def _overlay_entry(store: GraphStore, project_id: str, root: Path, file_path: Path, source: str) -> dict:
    indexer = JavaIndexer(store)
    return build_overlay_file_entry(
        store=store,
        project_id=project_id,
        project_path=str(root),
        file_path=str(file_path),
        source=source.encode("utf-8"),
        embed=False,
        base_method_catalog=indexer._existing_method_catalog(project_id),
        base_class_catalog=indexer._existing_class_catalog(project_id),
        base_class_ids_by_fqcn=indexer._existing_class_ids_by_fqcn(project_id),
        base_class_methods=indexer._existing_class_methods(project_id),
        existing_overlay_doc=store.overlay_store.load_project(project_id),
    )


def test_overlay_search_prefers_dirty_file_version(isolated_settings, tmp_path: Path):
    root = tmp_path / "project"
    java_file = root / "src" / "main" / "java" / "com" / "example" / "App.java"
    _write_java(
        java_file,
        """
        package com.example;
        public class App {
            public void greet() {}
        }
        """,
    )

    store = GraphStore(read_only=False)
    result = JavaIndexer(store).index_project(str(root), full=True)
    project_id = result.project_id

    entry = _overlay_entry(
        store,
        project_id,
        root,
        java_file,
        """
        package com.example;
        public class App {
            public void salute() {}
        }
        """,
    )
    store.overlay_store.upsert_file(
        project_id=project_id,
        project_path=str(root),
        repo_root=str(root),
        base_commit="base",
        current_head="base",
        file_path=str(java_file),
        entry=entry,
    )
    store.set_project_overlay_dirty(project_id, True)

    results = hybrid_search(store, "salute", project=project_id)
    assert any(item.get("name") == "salute" for item in results if isinstance(item, dict))

    old_results = hybrid_search(store, "greet", project=project_id)
    assert not any(item.get("name") == "greet" for item in old_results if isinstance(item, dict))


def test_overlay_deleted_file_suppresses_base_symbols(isolated_settings, tmp_path: Path):
    root = tmp_path / "project"
    java_file = root / "src" / "main" / "java" / "com" / "example" / "DeleteMe.java"
    _write_java(
        java_file,
        """
        package com.example;
        public class DeleteMe {}
        """,
    )

    store = GraphStore(read_only=False)
    result = JavaIndexer(store).index_project(str(root), full=True)
    project_id = result.project_id

    store.overlay_store.mark_deleted(
        project_id=project_id,
        project_path=str(root),
        repo_root=str(root),
        base_commit="base",
        current_head="base",
        file_path=str(java_file),
    )
    store.set_project_overlay_dirty(project_id, True)

    results = hybrid_search(store, "DeleteMe", project=project_id)
    assert not any(item.get("fqname") == "com.example.DeleteMe" for item in results if isinstance(item, dict))


def test_overlay_impact_includes_dirty_call_edges(isolated_settings, tmp_path: Path):
    root = tmp_path / "project"
    java_file = root / "src" / "main" / "java" / "com" / "example" / "App.java"
    _write_java(
        java_file,
        """
        package com.example;
        public class App {
            public void b() {}
        }
        """,
    )

    store = GraphStore(read_only=False)
    result = JavaIndexer(store).index_project(str(root), full=True)
    project_id = result.project_id

    entry = _overlay_entry(
        store,
        project_id,
        root,
        java_file,
        """
        package com.example;
        public class App {
            public void a() { b(); }
            public void b() {}
        }
        """,
    )
    store.overlay_store.upsert_file(
        project_id=project_id,
        project_path=str(root),
        repo_root=str(root),
        base_commit="base",
        current_head="base",
        file_path=str(java_file),
        entry=entry,
    )
    store.set_project_overlay_dirty(project_id, True)

    impact = analyze_impact(store, "b", project=project_id)
    direct = impact["depth_groups"]["1"]
    assert any(item.get("name") == "a" for item in direct)


def test_overlay_status_reports_promotion_pending(isolated_settings, tmp_path: Path):
    root = tmp_path / "project"
    java_file = root / "src" / "main" / "java" / "com" / "example" / "App.java"
    _write_java(
        java_file,
        """
        package com.example;
        public class App {
            public void greet() {}
        }
        """,
    )

    store = GraphStore(read_only=False)
    result = JavaIndexer(store).index_project(str(root), full=True)
    project_id = result.project_id
    store.set_project_indexed_commit(project_id, "base")

    entry = _overlay_entry(
        store,
        project_id,
        root,
        java_file,
        """
        package com.example;
        public class App {
            public void salute() {}
        }
        """,
    )
    store.overlay_store.upsert_file(
        project_id=project_id,
        project_path=str(root),
        repo_root=str(root),
        base_commit="base",
        current_head="head",
        file_path=str(java_file),
        entry=entry,
    )
    store.set_project_overlay_dirty(project_id, True)

    status = get_overlay_status(store, project=project_id)
    assert len(status) == 1
    assert status[0]["dirty_file_count"] == 1
    assert status[0]["overlay_dirty"] is True
    assert status[0]["promotion_pending"] is True
