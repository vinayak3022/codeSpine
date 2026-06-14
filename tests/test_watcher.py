from __future__ import annotations

from pathlib import Path

from codespine.watch.watcher import _reindex_commit_change, _update_files_in_graph


class _Store:
    def __init__(self):
        self.indexed_commits: list[tuple[str, str]] = []
        self.snapshots: list[bool] = []

    def set_project_indexed_commit(self, project_id: str, head: str):
        self.indexed_commits.append((project_id, head))

    def snapshot_to_read_replica(self, background: bool = False):
        self.snapshots.append(background)


def test_reindex_commit_change_retries_after_partial_failure(monkeypatch):
    calls: list[tuple[str, str, tuple[str, ...]]] = []
    outcomes = iter([
        {"changed": 1, "deleted": 0, "errors": 1},
        {"changed": 1, "deleted": 0, "errors": 0},
    ])

    monkeypatch.setattr("codespine.watch.watcher._git_changed_files", lambda *args, **kwargs: ["/repo/module/src/Foo.java"])
    monkeypatch.setattr(
        "codespine.watch.watcher._update_files_in_graph",
        lambda store, module_path, project_id, files: calls.append((module_path, project_id, tuple(files))) or next(outcomes),
    )

    store = _Store()
    head_state = {"value": "old"}
    module_map = {"/repo/module": "module-project"}
    sorted_modules = ["/repo/module"]

    first = _reindex_commit_change(
        store,
        "/repo",
        "/repo",
        module_map,
        sorted_modules,
        head_state,
        "new",
        True,
    )

    assert store.indexed_commits == []
    assert store.snapshots == []

    second = _reindex_commit_change(
        store,
        "/repo",
        "/repo",
        module_map,
        sorted_modules,
        head_state,
        "new",
        True,
    )

    assert first is False
    assert second is True
    assert head_state["value"] == "new"
    assert store.indexed_commits == [("module-project", "new")]
    assert store.snapshots == [True]
    assert calls == [
        ("/repo/module", "module-project", ("/repo/module/src/Foo.java",)),
        ("/repo/module", "module-project", ("/repo/module/src/Foo.java",)),
    ]


def test_reindex_commit_change_advances_head_after_success(monkeypatch):
    monkeypatch.setattr("codespine.watch.watcher._git_changed_files", lambda *args, **kwargs: [])

    store = _Store()
    head_state = {"value": "old"}

    success = _reindex_commit_change(
        store,
        "/repo",
        "/repo",
        {"/repo": "repo"},
        ["/repo"],
        head_state,
        "new",
        True,
    )

    assert success is True
    assert head_state["value"] == "new"
    assert store.indexed_commits == [("repo", "new")]
    assert store.snapshots == [True]


def test_reindex_commit_change_fails_safe_on_git_diff_error(monkeypatch):
    monkeypatch.setattr("codespine.watch.watcher._git_changed_files", lambda *args, **kwargs: None)

    store = _Store()
    head_state = {"value": "old"}

    success = _reindex_commit_change(
        store,
        "/repo",
        "/repo",
        {"/repo": "repo"},
        ["/repo"],
        head_state,
        "new",
        True,
    )

    assert success is False
    assert head_state["value"] == "old"
    assert store.indexed_commits == []
    assert store.snapshots == []


def test_update_files_in_graph_refreshes_catalogs_after_deletion(monkeypatch, tmp_path: Path):
    deleted_path = tmp_path / "A.java"
    changed_path = tmp_path / "B.java"
    changed_path.write_text("class B {}", encoding="utf-8")

    initial_method_catalog = {
        "deleted-method": {
            "signature": "deleted()",
            "name": "deleted",
            "param_count": 0,
            "class_fqcn": "example.Deleted",
            "class_id": "class-deleted",
        }
    }
    refreshed_method_catalog = {}
    initial_class_catalog = {"Deleted": ["example.Deleted"]}
    refreshed_class_catalog = {}
    initial_class_ids = {"example.Deleted": ["class-deleted"]}
    refreshed_class_ids = {}
    initial_class_methods = {"class-deleted": {"deleted()": "deleted-method"}}
    refreshed_class_methods = {}

    method_catalogs = iter([initial_method_catalog, refreshed_method_catalog])
    class_catalogs = iter([initial_class_catalog, refreshed_class_catalog])
    class_ids = iter([initial_class_ids, refreshed_class_ids])
    class_methods = iter([initial_class_methods, refreshed_class_methods])
    seen_catalogs: list[dict[str, object]] = []

    class _Indexer:
        def __init__(self, store):
            self.store = store

        def _existing_method_catalog(self, project_id: str):
            return next(method_catalogs)

        def _existing_class_catalog(self, project_id: str):
            return next(class_catalogs)

        def _existing_class_ids_by_fqcn(self, project_id: str):
            return next(class_ids)

        def _existing_class_methods(self, project_id: str):
            return next(class_methods)

        def project_has_embeddings(self, project_id: str):
            return False

    class _Store:
        def __init__(self):
            self.upserts: list[str] = []
            self.clears: list[str] = []
            self.projects: list[tuple[str, str]] = []

        def get_project_metadata(self, project_id: str):
            return None

        def upsert_project(self, project_id: str, project_path: str):
            self.projects.append((project_id, project_path))

        def upsert_file_from_entry(self, entry, project_path: str):
            self.upserts.append(entry["file_path"])

        def clear_file_by_path(self, project_id: str, project_path: str, file_path: str):
            self.clears.append(file_path)

    def fake_build_overlay_file_entry(**kwargs):
        seen_catalogs.append(
            {
                "methods": dict(kwargs["base_method_catalog"]),
                "classes": {k: list(v) for k, v in kwargs["base_class_catalog"].items()},
                "class_ids": {k: list(v) for k, v in kwargs["base_class_ids_by_fqcn"].items()},
                "class_methods": {k: dict(v) for k, v in kwargs["base_class_methods"].items()},
            }
        )
        return {"file_path": kwargs["file_path"], "symbols": []}

    monkeypatch.setattr("codespine.watch.watcher.JavaIndexer", _Indexer)
    monkeypatch.setattr("codespine.watch.watcher.build_overlay_file_entry", fake_build_overlay_file_entry)

    store = _Store()
    result = _update_files_in_graph(store, str(tmp_path), "project", [str(deleted_path), str(changed_path)])

    assert result == {"project_id": "project", "changed": 1, "deleted": 1, "errors": 0}
    assert store.clears == [str(deleted_path)]
    assert store.upserts == [str(changed_path)]
    assert seen_catalogs == [
        {
            "methods": refreshed_method_catalog,
            "classes": refreshed_class_catalog,
            "class_ids": refreshed_class_ids,
            "class_methods": refreshed_class_methods,
        }
    ]
