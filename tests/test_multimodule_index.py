from pathlib import Path

import pytest

pytest.importorskip("kuzu")
pytest.importorskip("tree_sitter_java")

from codespine.db.store import GraphStore
from codespine.indexer.engine import JavaIndexer


def _write_java(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_multimodule_duplicate_fqcn_is_indexed_without_collision(tmp_path: Path):
    _write_java(
        tmp_path / "module-a" / "src" / "main" / "java" / "com" / "example" / "App.java",
        """
        package com.example;
        public class App { public void fromA() {} }
        """,
    )
    _write_java(
        tmp_path / "module-b" / "src" / "main" / "java" / "com" / "example" / "App.java",
        """
        package com.example;
        public class App { public void fromB() {} }
        """,
    )

    store = GraphStore(read_only=False)
    result = JavaIndexer(store).index_project(str(tmp_path), full=True)

    classes = store.query_records(
        """
        MATCH (c:Class), (f:File)
        WHERE c.file_id = f.id AND f.project_id = $pid AND c.fqcn = $fqcn
        RETURN c.id as id, f.path as path
        """,
        {"pid": result.project_id, "fqcn": "com.example.App"},
    )
    methods = store.query_records(
        """
        MATCH (m:Method), (c:Class), (f:File)
        WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
        RETURN m.name as name
        """,
        {"pid": result.project_id},
    )

    assert len(classes) == 2
    assert len({c["id"] for c in classes}) == 2
    assert {"fromA", "fromB"}.issubset({m["name"] for m in methods})
