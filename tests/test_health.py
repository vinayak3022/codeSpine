from __future__ import annotations

from codespine.health import graph_integrity_checks, index_health, project_health, smoke_test_index


class FakeStore:
    def __init__(self, counts: dict[str, int] | None = None, fail_on: str | None = None):
        self.counts = counts or {}
        self.fail_on = fail_on
        self.queries: list[str] = []

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(query)
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("boom")
        if "as total" in query:
            if "MATCH (f:File)" in query:
                return [{"total": self.counts.get("files_total", 0)}]
            if "MATCH (c:Class)" in query:
                return [{"total": self.counts.get("classes_total", 0)}]
            if "MATCH (m:Method)" in query:
                return [{"total": self.counts.get("methods_total", 0)}]
            if "MATCH (s:Symbol)" in query:
                return [{"total": self.counts.get("symbols_total", 0)}]
        if "as linked" in query:
            if "MATCH (f:File), (p:Project)" in query:
                return [{"linked": self.counts.get("files_linked", 0)}]
            if "MATCH (c:Class), (f:File)" in query:
                return [{"linked": self.counts.get("classes_linked", 0)}]
            if "MATCH (m:Method), (c:Class)" in query:
                return [{"linked": self.counts.get("methods_linked", 0)}]
            if "MATCH (s:Symbol), (f:File)" in query:
                return [{"linked": self.counts.get("symbols_linked", 0)}]
        if "count(DISTINCT m.id)" in query:
            return [{"n": self.counts.get("methods_with_outgoing", 0)}]
        if "CALLS" in query:
            return [{"n": self.counts.get("calls", 0)}]
        if "MATCH (m:Method)" in query:
            return [{"n": self.counts.get("methods", 0)}]
        if "MATCH (c:Class)" in query:
            return [{"n": self.counts.get("classes", 0)}]
        if "MATCH (f:File)" in query:
            return [{"n": self.counts.get("files", 0)}]
        return [{"n": self.counts.get("default", 0)}]


class FakeRouter:
    def shard_for(self, project_id: str) -> int:
        return 2


class FakeShardedStore:
    router = FakeRouter()

    def __init__(self, shard: FakeStore):
        self._shard = shard

    def list_project_metadata(self) -> list[dict]:
        return [{"id": "app", "path": "/tmp/app"}]

    def shard(self, project_id: str) -> FakeStore:
        assert project_id == "app"
        return self._shard

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        return self._shard.query_records(query, params)


class FakeFanoutStore(FakeShardedStore):
    def __init__(self):
        self._shards = {
            "app-a": FakeStore({"files": 40, "classes": 80, "methods": 90, "calls": 0, "methods_with_outgoing": 0}),
            "app-b": FakeStore({"files": 30, "classes": 60, "methods": 95, "calls": 0, "methods_with_outgoing": 0}),
        }

    def list_project_metadata(self) -> list[dict]:
        return [
            {"id": "app-a", "path": "/tmp/app-a"},
            {"id": "app-b", "path": "/tmp/app-b"},
        ]

    def shard(self, project_id: str) -> FakeStore:
        return self._shards[project_id]

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "MATCH (f:File) RETURN count(f) as total" in query:
            return [{"total": 2}, {"total": 3}]
        if "MATCH (f:File), (p:Project)" in query:
            return [{"linked": 1}, {"linked": 2}]
        if "MATCH (c:Class) RETURN count(c) as total" in query:
            return [{"total": 4}, {"total": 5}]
        if "MATCH (c:Class), (f:File)" in query:
            return [{"linked": 3}, {"linked": 4}]
        if "MATCH (m:Method) RETURN count(m) as total" in query:
            return [{"total": 6}, {"total": 7}]
        if "MATCH (m:Method), (c:Class)" in query:
            return [{"linked": 4}, {"linked": 5}]
        if "MATCH (s:Symbol) RETURN count(s) as total" in query:
            return [{"total": 8}, {"total": 9}]
        if "MATCH (s:Symbol), (f:File)" in query:
            return [{"linked": 6}, {"linked": 7}]
        return [{"n": 0}]


def test_project_health_flags_zero_call_edges_for_large_project():
    store = FakeStore({"files": 10, "classes": 20, "methods": 150, "calls": 0, "methods_with_outgoing": 0})

    health = project_health(store, "app")

    assert health["methods"] == 150
    assert health["calls"] == 0
    assert health["call_edge_coverage"] == 0
    assert health["anomalies"][0]["code"] == "zero_call_edges"


def test_index_health_aggregates_project_anomalies():
    shard = FakeStore({"files": 10, "classes": 20, "methods": 150, "calls": 0, "methods_with_outgoing": 0})
    store = FakeShardedStore(shard)

    health = index_health(store)

    assert health["summary"] == {
        "project_count": 1,
        "anomaly_count": 1,
        "critical_count": 1,
        "state_counts": {"ready": 1},
    }
    assert health["projects"][0]["shard"] == 2
    assert health["graph_integrity"]["issue_count"] == 0


def test_graph_integrity_checks_report_dangling_relations():
    store = FakeStore(
        {
            "files_total": 4,
            "files_linked": 3,
            "classes_total": 5,
            "classes_linked": 4,
            "methods_total": 8,
            "methods_linked": 6,
            "symbols_total": 6,
            "symbols_linked": 5,
        }
    )

    integrity = graph_integrity_checks(store)

    assert integrity["issue_count"] == 4
    assert [item["code"] for item in integrity["issues"]] == [
        "files_without_project",
        "classes_without_file",
        "methods_without_class",
        "symbols_without_file",
    ]
    assert integrity["checks"]["methods_without_class"]["dangling"] == 2


def test_index_health_aggregates_across_shards(monkeypatch):
    store = FakeFanoutStore()
    monkeypatch.setattr(
        "codespine.health.snapshot_info",
        lambda project_id, router=None: {"snapshot_valid": True, "write_db_valid": True, "shard": 0 if project_id == "app-a" else 1},
    )

    health = index_health(store)

    assert health["summary"] == {
        "project_count": 2,
        "anomaly_count": 0,
        "critical_count": 0,
        "state_counts": {"ready": 2},
    }
    assert health["graph_integrity"]["checks"]["files_without_project"] == {
        "total": 5,
        "linked": 3,
        "dangling": 2,
    }
    assert health["graph_integrity"]["checks"]["classes_without_file"] == {
        "total": 9,
        "linked": 7,
        "dangling": 2,
    }
    assert health["graph_integrity"]["issue_count"] == 4


def test_smoke_test_index_reports_query_failures():
    store = FakeStore(fail_on="CONTAINS")

    result = smoke_test_index(store)

    assert result["ok"] is False
    assert result["failed_count"] == 1
    assert result["checks"][2]["name"] == "contains_lower_rhs"
