from __future__ import annotations


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            ins = curr[j - 1] + 1
            delete = prev[j] + 1
            repl = prev[j - 1] + (0 if ca == cb else 1)
            curr.append(min(ins, delete, repl))
        prev = curr
    return prev[-1]


def normalized_similarity(a: str, b: str) -> float:
    a_l = (a or "").lower()
    b_l = (b or "").lower()
    if not a_l and not b_l:
        return 1.0
    dist = levenshtein(a_l, b_l)
    return 1.0 - (dist / max(len(a_l), len(b_l), 1))


def rank_fuzzy(query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]:
    ranked = [(doc_id, normalized_similarity(query, text)) for doc_id, text in docs]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked
