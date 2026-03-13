from codespine.analysis.deadcode import detect_dead_code


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
