from codespine.analysis.community import detect_communities


class _CommunityStore:
    def __init__(self) -> None:
        self.persisted: list[tuple[str, str, float, list[str]]] = []
        self.cleared = 0

    def query_records(self, query: str, params=None):
        if "MATCH (s:Symbol)" in query and "RETURN s.id as id" in query:
            return [
                {"id": "class_a", "kind": "class", "fqname": "com.example.alpha.ServiceA", "file_id": "f1"},
                {"id": "m_a", "kind": "method", "fqname": "com.example.alpha.ServiceA#run()", "file_id": "f1"},
                {"id": "class_b", "kind": "class", "fqname": "com.example.alpha.ServiceB", "file_id": "f2"},
                {"id": "m_b", "kind": "method", "fqname": "com.example.alpha.ServiceB#run()", "file_id": "f2"},
                {"id": "class_c", "kind": "class", "fqname": "com.example.beta.ServiceC", "file_id": "f3"},
                {"id": "m_c", "kind": "method", "fqname": "com.example.beta.ServiceC#run()", "file_id": "f3"},
            ]
        if "MATCH (m:Method), (c:Class)" in query:
            return [
                {"method_id": "mm_a", "file_id": "f1", "class_fqcn": "com.example.alpha.ServiceA", "signature": "run()"},
                {"method_id": "mm_b", "file_id": "f2", "class_fqcn": "com.example.alpha.ServiceB", "signature": "run()"},
                {"method_id": "mm_c", "file_id": "f3", "class_fqcn": "com.example.beta.ServiceC", "signature": "run()"},
            ]
        if "MATCH (a:Method)-[:CALLS]->(b:Method)" in query:
            return [{"src": "mm_a", "dst": "mm_b"}]
        return []

    def clear_communities(self) -> None:
        self.cleared += 1

    def set_community(self, community_id: str, label: str, cohesion: float, symbol_ids: list[str]) -> None:
        self.persisted.append((community_id, label, cohesion, list(symbol_ids)))


def test_detect_communities_merges_sparse_singletons_into_package_groups():
    store = _CommunityStore()
    communities = detect_communities(store)

    assert store.cleared == 1
    assert len(communities) <= 2
    assert all(c["size"] >= 2 for c in communities)
    labels = {c["label"] for c in communities}
    assert "com.example.alpha" in labels or "com.example.beta" in labels
