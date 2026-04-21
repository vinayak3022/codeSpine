from pathlib import Path

import pytest

from codespine.analysis.deadcode import detect_dead_code

pytest.importorskip("duckdb")
pytest.importorskip("tree_sitter_java")

from codespine.db.duckdb_store import DuckDBStore


class _DeadCodeStore:
    def query_records(self, query: str, params=None):
        if "AND NOT EXISTS { MATCH (:Method)-[:CALLS]->(m) }" in query:
            return [
                {
                    "method_id": "controller_method",
                    "name": "handle",
                    "signature": "handle()",
                    "modifiers": ["public"],
                    "class_fqcn": "com.example.PaymentController",
                    "is_constructor": False,
                    "is_test": False,
                    "file_path": "/repo/src/main/java/com/example/controller/PaymentController.java",
                },
                {
                    "method_id": "base_contract",
                    "name": "findAll",
                    "signature": "findAll()",
                    "modifiers": ["public"],
                    "class_fqcn": "com.example.PaymentRepository",
                    "is_constructor": False,
                    "is_test": False,
                    "file_path": "/repo/src/main/java/com/example/repository/PaymentRepository.java",
                },
                {
                    "method_id": "plain_dead",
                    "name": "helper",
                    "signature": "helper()",
                    "modifiers": ["private"],
                    "class_fqcn": "com.example.PaymentUtils",
                    "is_constructor": False,
                    "is_test": False,
                    "file_path": "/repo/src/main/java/com/example/PaymentUtils.java",
                },
            ]
        if "MATCH (m:Method)-[:OVERRIDES]->(:Method)" in query:
            return []
        if "MATCH (:Method)-[:OVERRIDES]->(m:Method)" in query:
            return [{"method_id": "base_contract"}]
        return []


def test_deadcode_exempts_framework_roles_and_base_contracts():
    result = detect_dead_code(_DeadCodeStore(), limit=20, strict=False)
    dead_ids = {item["method_id"] for item in result if "_stats" not in item}

    assert "plain_dead" in dead_ids
    assert "controller_method" not in dead_ids
    assert "base_contract" not in dead_ids


def test_deadcode_executes_against_real_duckdb_store(tmp_path: Path):
    store = DuckDBStore(
        db_path_override=str(tmp_path / "db"),
        snapshot_path_override=str(tmp_path / "db_read"),
    )
    store.upsert_project("app", "/app")
    store.upsert_file("f1", "/app/src/App.java", "app", False, "abc")
    store.upsert_class("c1", "com.example.App", "App", "com.example", "f1")
    store.upsert_methods_batch(
        [
            {
                "id": "m1",
                "class_id": "c1",
                "name": "used",
                "signature": "used():void",
                "return_type": "void",
                "modifiers": ["public"],
                "is_constructor": False,
                "is_test": False,
            },
            {
                "id": "m2",
                "class_id": "c1",
                "name": "dead",
                "signature": "dead():void",
                "return_type": "void",
                "modifiers": ["private"],
                "is_constructor": False,
                "is_test": False,
            },
        ]
    )
    store.add_calls_batch(
        [{"source_id": "m_ext", "target_id": "m1", "confidence": 1.0, "reason": "direct"}]
    )

    result = detect_dead_code(store, limit=10, project="app", strict=True)

    dead_ids = {item["method_id"] for item in result if "_stats" not in item}
    assert "m2" in dead_ids
    assert "m1" not in dead_ids
