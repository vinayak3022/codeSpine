from codespine.search.fuzzy import normalized_similarity
from codespine.search.rrf import reciprocal_rank_fusion


def test_fuzzy_prefers_closer_typo():
    assert normalized_similarity("procssPayment", "processPayment") > normalized_similarity(
        "procssPayment", "helper"
    )


def test_rrf_combines_rankings():
    rank1 = [("a", 1.0), ("b", 0.9)]
    rank2 = [("b", 1.0), ("a", 0.9)]
    out = reciprocal_rank_fusion([rank1, rank2], k=60)
    ids = [i for i, _ in out]
    assert ids[0] in {"a", "b"}
    assert set(ids[:2]) == {"a", "b"}
