from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("kuzu")
pytest.importorskip("tree_sitter_java")

from codespine.config import SETTINGS
from codespine.db.store import GraphStore
from codespine.indexer.engine import JavaIndexer
from codespine.overlay.store import build_overlay_file_entry
from codespine.overlay.merge import merged_call_edges
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


def test_overlay_project_scoped_search_ignores_out_of_project_dirty_symbols(isolated_settings, tmp_path: Path):
    class _OverlayStore:
        def load_project(self, project: str):
            return {
                "project_id": project,
                "project_path": "/repo/app",
                "dirty_files": {
                    "/repo/other/src/main/java/com/example/Foo.java": {
                        "file_id": "f_other",
                        "symbols": [
                            {
                                "id": "s_other",
                                "kind": "class",
                                "name": "Foo",
                                "fqname": "com.other.Foo",
                                "line": 1,
                                "col": 1,
                                "file_id": "f_other",
                                "file_path": "/repo/other/src/main/java/com/example/Foo.java",
                                "project_id": "other",
                                "is_test": False,
                            }
                        ],
                    },
                    "/repo/app/src/main/java/com/example/Foo.java": {
                        "file_id": "f_app",
                        "symbols": [
                            {
                                "id": "s_app",
                                "kind": "class",
                                "name": "Foo",
                                "fqname": "com.example.Foo",
                                "line": 1,
                                "col": 1,
                                "file_id": "f_app",
                                "file_path": "/repo/app/src/main/java/com/example/Foo.java",
                                "project_id": "app",
                                "is_test": False,
                            }
                        ],
                    },
                },
                "deleted_files": [],
            }

    class _OverlayAwareStore:
        overlay_store = _OverlayStore()

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            return []

    results = hybrid_search(_OverlayAwareStore(), "Foo", project="app", k=10)

    assert results
    assert all(item.get("file_path", "").startswith("/repo/app/") for item in results)
    assert all(item.get("fqname") == "com.example.Foo" for item in results)


def test_overlay_project_scoped_impact_filters_cross_project_and_deleted_call_edges(isolated_settings, tmp_path: Path):
    class _OverlayStore:
        def load_project(self, project: str):
            return {
                "project_id": project,
                "project_path": "/repo/app",
                "dirty_files": {
                    "/repo/app/src/main/java/com/example/App.java": {
                        "file_id": "f_app",
                        "symbols": [
                            {
                                "id": "s_target",
                                "kind": "method",
                                "name": "target",
                                "fqname": "com.example.App#target()",
                                "line": 1,
                                "col": 1,
                                "file_id": "f_app",
                                "file_path": "/repo/app/src/main/java/com/example/App.java",
                                "project_id": "app",
                                "is_test": False,
                            },
                                {
                                    "id": "s_caller",
                                    "kind": "method",
                                    "name": "caller",
                                    "fqname": "com.example.Caller#caller()",
                                    "line": 2,
                                    "col": 1,
                                    "file_id": "f_app",
                                    "file_path": "/repo/app/src/main/java/com/example/App.java",
                                "project_id": "app",
                                "is_test": False,
                            },
                        ],
                        "methods": [
                            {
                                "id": "m_target",
                                "class_id": "c_app",
                                "class_fqcn": "com.example.App",
                                "name": "target",
                                "signature": "target()",
                                "return_type": "void",
                                "is_constructor": False,
                                "is_test": False,
                                "file_id": "f_app",
                                "file_path": "/repo/app/src/main/java/com/example/App.java",
                                "project_id": "app",
                            },
                                {
                                    "id": "m_caller",
                                    "class_id": "c_caller",
                                    "class_fqcn": "com.example.Caller",
                                    "name": "caller",
                                    "signature": "caller()",
                                    "return_type": "void",
                                    "is_constructor": False,
                                    "is_test": False,
                                "file_id": "f_app",
                                "file_path": "/repo/app/src/main/java/com/example/App.java",
                                "project_id": "app",
                            },
                        ],
                        "calls": [
                            {"src": "m_caller", "dst": "m_deleted", "confidence": 0.9, "reason": "deleted-target"},
                        ],
                    }
                },
                "deleted_files": ["/repo/app/src/main/java/com/example/Deleted.java"],
            }

        def list_projects(self):
            return [self.load_project("app")]

    class _OverlayAwareStore:
        overlay_store = _OverlayStore()

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (s:Symbol), (f:File)" in query and "f.project_id = $proj" in query:
                return [
                    {
                        "id": "s_target",
                        "kind": "method",
                        "name": "target",
                        "fqname": "com.example.App#target()",
                        "embedding": None,
                        "line": 1,
                        "col": 1,
                        "file_id": "f_app",
                        "file_path": "/repo/app/src/main/java/com/example/App.java",
                        "project_id": "app",
                        "is_test": False,
                    }
                ]
            if "MATCH (m:Method), (c:Class), (f:File)" in query and "f.project_id = $proj" in query and "RETURN m.id as id" in query:
                return [
                    {
                        "id": "m_target",
                        "class_id": "c_app",
                        "class_fqcn": "com.example.App",
                        "name": "target",
                        "signature": "target()",
                        "return_type": "void",
                        "is_constructor": False,
                        "is_test": False,
                        "file_id": "f_app",
                        "file_path": "/repo/app/src/main/java/com/example/App.java",
                        "project_id": "app",
                    },
                        {
                            "id": "m_caller",
                            "class_id": "c_caller",
                            "class_fqcn": "com.example.Caller",
                            "name": "caller",
                            "signature": "caller()",
                            "return_type": "void",
                            "is_constructor": False,
                        "is_test": False,
                        "file_id": "f_app",
                        "file_path": "/repo/app/src/main/java/com/example/App.java",
                        "project_id": "app",
                    },
                ]
            if "RETURN a.id as src" in query and "b.id as dst" in query and "CALLS" in query:
                return [
                    {"src": "m_caller", "dst": "m_target", "src_file_id": "f_app", "dst_file_id": "f_app", "edge_type": "CALLS", "confidence": 0.9, "reason": "project"},
                    {"src": "m_external", "dst": "m_target", "src_file_id": "f_other", "dst_file_id": "f_app", "edge_type": "CALLS", "confidence": 0.9, "reason": "cross-project"},
                ]
            return []

    impact = analyze_impact(_OverlayAwareStore(), "target", project="app")

    direct = impact["impacted_callers"]["1"]
    assert [item.get("symbol") for item in direct] == ["m_caller"]
    assert all(item.get("project_id") == "app" for item in direct)


def test_overlay_merged_call_edges_drop_deleted_and_cross_project_targets(isolated_settings, tmp_path: Path):
    class _OverlayStore:
        def load_project(self, project: str):
            return {
                "project_id": project,
                "project_path": "/repo/app",
                "dirty_files": {
                    "/repo/app/src/main/java/com/example/App.java": {
                        "file_id": "f_app",
                        "methods": [
                            {
                                "id": "m_target",
                                "class_id": "c_caller",
                                "class_fqcn": "com.example.App",
                                "name": "target",
                                "signature": "target()",
                                "return_type": "void",
                                "is_constructor": False,
                                "is_test": False,
                                "file_id": "f_app",
                                "file_path": "/repo/app/src/main/java/com/example/App.java",
                                "project_id": "app",
                            },
                            {
                                "id": "m_caller",
                                "class_id": "c_caller",
                                "class_fqcn": "com.example.App",
                                "name": "caller",
                                "signature": "caller()",
                                "return_type": "void",
                                "is_constructor": False,
                                "is_test": False,
                                "file_id": "f_app",
                                "file_path": "/repo/app/src/main/java/com/example/App.java",
                                "project_id": "app",
                            },
                        ],
                        "calls": [
                            {"src": "m_caller", "dst": "m_target", "confidence": 0.9, "reason": "project"},
                            {"src": "m_caller", "dst": "m_deleted", "confidence": 0.9, "reason": "deleted"},
                            {"src": "m_caller", "dst": "m_external", "confidence": 0.9, "reason": "cross-project-target"},
                        ],
                    },
                    "/repo/other/src/main/java/com/example/Other.java": {
                        "file_id": "f_other",
                        "methods": [
                            {
                                "id": "m_external",
                                "class_id": "c_other",
                                "class_fqcn": "com.other.Other",
                                "name": "external",
                                "signature": "external()",
                                "return_type": "void",
                                "is_constructor": False,
                                "is_test": False,
                                "file_id": "f_other",
                                "file_path": "/repo/other/src/main/java/com/example/Other.java",
                                "project_id": "other",
                            }
                        ],
                        "calls": [
                            {"src": "m_external", "dst": "m_target", "confidence": 0.9, "reason": "outside-project"},
                        ],
                    },
                },
                "deleted_files": ["/repo/app/src/main/java/com/example/Deleted.java"],
            }

    class _OverlayAwareStore:
        overlay_store = _OverlayStore()

        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (m:Method), (c:Class), (f:File)" in query and "f.project_id = $proj" in query and "RETURN m.id as id" in query:
                return [
                    {
                        "id": "m_target",
                        "class_id": "c_caller",
                        "class_fqcn": "com.example.App",
                        "name": "target",
                        "signature": "target()",
                        "return_type": "void",
                        "is_constructor": False,
                        "is_test": False,
                        "file_id": "f_app",
                        "file_path": "/repo/app/src/main/java/com/example/App.java",
                        "project_id": "app",
                    },
                    {
                        "id": "m_caller",
                        "class_id": "c_caller",
                        "class_fqcn": "com.example.App",
                        "name": "caller",
                        "signature": "caller()",
                        "return_type": "void",
                        "is_constructor": False,
                        "is_test": False,
                        "file_id": "f_app",
                        "file_path": "/repo/app/src/main/java/com/example/App.java",
                        "project_id": "app",
                    },
                ]
            if "RETURN a.id as src" in query and "b.id as dst" in query and "CALLS" in query:
                return [
                    {"src": "m_caller", "dst": "m_target", "src_file_id": "f_app", "dst_file_id": "f_app", "confidence": 0.9, "reason": "project"},
                    {"src": "m_external", "dst": "m_target", "src_file_id": "f_other", "dst_file_id": "f_app", "confidence": 0.9, "reason": "cross-project"},
                    {"src": "m_caller", "dst": "m_deleted", "src_file_id": "f_app", "dst_file_id": "f_deleted", "confidence": 0.9, "reason": "deleted"},
                ]
            return []

    edges = merged_call_edges(_OverlayAwareStore(), _OverlayAwareStore().overlay_store, project="app")

    assert len(edges) == 2
    assert {(edge["src"], edge["dst"]) for edge in edges} == {("m_caller", "m_target")}
    assert all(edge["src_file_id"] == "f_app" for edge in edges)
    assert all(edge["dst_file_id"] == "f_app" for edge in edges)
    assert all(edge["src_project_id"] == "app" for edge in edges)
    assert all(edge["dst_project_id"] == "app" for edge in edges)
    assert all(edge["edge_type"] == "CALLS" for edge in edges)


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
    # FR-06: same-class callers are now in self_callers; cross-class callers in impacted_callers["1"]
    all_direct = impact["impacted_callers"]["1"] + impact.get("self_callers", [])
    assert any(item.get("name") == "a" for item in all_direct)


def test_overlay_impact_metadata_resolution_honors_project(monkeypatch):
    class _Store:
        overlay_store = object()

    def fake_merged_symbol_records(store, overlay_store, project: str | None = None):
        return [
            {"id": "s_target", "kind": "method", "name": "target", "fqname": "com.example.App#target()", "file_id": "f_app", "file_path": "/repo/app/App.java", "project_id": "app"},
            {"id": "s_caller", "kind": "method", "name": "caller", "fqname": "com.example.App#caller()", "file_id": "f_app", "file_path": "/repo/app/App.java", "project_id": "app"},
        ]

    def fake_merged_method_records(store, overlay_store, project: str | None = None):
        if project == "app":
            return [
                {"id": "m_target", "class_fqcn": "com.example.App", "signature": "target()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java", "name": "target"},
                {"id": "m_hash", "class_fqcn": "com.other.Other", "signature": "caller()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java", "name": "caller"},
            ]
        return [
            {"id": "m_target", "class_fqcn": "com.example.App", "signature": "target()", "file_id": "f_app", "project_id": "app", "file_path": "/repo/app/App.java", "name": "target"},
            {"id": "m_hash", "class_fqcn": "com.other.Other", "signature": "caller()", "file_id": "f_other", "project_id": "other", "file_path": "/repo/other/Other.java", "name": "caller"},
        ]

    def fake_merged_call_edges(store, overlay_store, project: str | None = None):
        return [{"src": "m_hash", "dst": "m_target", "confidence": 0.9, "reason": "project", "edge_type": "CALLS"}]

    monkeypatch.setattr("codespine.analysis.impact.merged_symbol_records", fake_merged_symbol_records)
    monkeypatch.setattr("codespine.analysis.impact.merged_method_records", fake_merged_method_records)
    monkeypatch.setattr("codespine.analysis.impact.merged_call_edges", fake_merged_call_edges)

    impact = analyze_impact(_Store(), "target", project="app")

    assert impact["impacted_callers"]["1"][0]["project_id"] == "app"
    assert impact["impacted_callers"]["1"][0]["file_path"] == "/repo/app/App.java"


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
