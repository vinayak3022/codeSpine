from __future__ import annotations

from codespine.analysis.coupling import get_coupling


class FakeStore:
    def __init__(self, recs: list[dict] | None = None):
        self.recs = recs or []
        self.queries: list[tuple[str, dict | None]] = []

    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        self.queries.append((query, params))
        return self.recs


def test_get_coupling_symbol_applies_thresholds_to_all_symbol_matches():
    store = FakeStore([{"file": "a", "coupled_file": "b", "strength": 0.9, "cochanges": 5}])

    result = get_coupling(store, symbol="Foo", min_strength=0.8, min_cochanges=4)

    assert result == {"symbol": "Foo", "couplings": store.recs}
    query, params = store.queries[0]
    assert "WHERE (s.id = $q OR lower(s.fqname) = lower($q) OR lower(s.name) = lower($q))" in query
    assert "AND r.strength >= $min_strength AND r.cochanges >= $min_cochanges" in query
    assert params == {"q": "Foo", "min_strength": 0.8, "min_cochanges": 4}


def test_get_coupling_project_filters_file_reads():
    store = FakeStore([{"file": "a", "coupled_file": "b", "strength": 0.9, "cochanges": 5}])

    result = get_coupling(store, project="app", days=7)

    assert result == {"symbol": None, "couplings": store.recs}
    query, params = store.queries[0]
    assert "AND f.project_id = $proj AND f2.project_id = $proj" in query
    assert params == {"days": 7, "min_strength": 0.3, "min_cochanges": 3, "proj": "app"}
