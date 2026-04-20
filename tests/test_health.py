from __future__ import annotations

from codespine.health import index_health, project_health, smoke_test_index


class FakeStore:
    def __init__(self, counts: dict[str, int] | None = None, fail_on: str | None = None):
        self.counts = counts or {}
        self.fail_on = fail_on
        self.queries: list[str] = []

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append(query)
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("boom")
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

    assert health["summary"] == {"project_count": 1, "anomaly_count": 1, "critical_count": 1}
    assert health["projects"][0]["shard"] == 2


def test_smoke_test_index_reports_query_failures():
    store = FakeStore(fail_on="CONTAINS")

    result = smoke_test_index(store)

    assert result["ok"] is False
    assert result["failed_count"] == 1
    assert result["checks"][2]["name"] == "contains_lower_rhs"

