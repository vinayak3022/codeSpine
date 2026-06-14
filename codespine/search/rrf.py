from __future__ import annotations


def reciprocal_rank_fusion(
    rankings: list[list[tuple[str, float]]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """Weighted reciprocal rank fusion.

    Parameters
    ----------
    rankings:
        List of rankers' results, each a list of (doc_id, score) sorted
        descending by score.
    k:
        RRF constant. Higher values give more weight to deep ranking positions.
    weights:
        Per-ranker weight multipliers.  When provided, ``len(weights)`` must
        equal ``len(rankings)``.  When *None*, all rankers have equal weight.
    """
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(
            f"RRF got {len(weights)} weights for {len(rankings)} rankers"
        )
    scores: dict[str, float] = {}
    for i, ranking in enumerate(rankings):
        w = weights[i] if weights else 1.0
        for rank, (doc_id, _) in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
