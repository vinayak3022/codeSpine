from __future__ import annotations

import hashlib
import math
from functools import lru_cache

from codespine.config import SETTINGS


def _hash_vector(text: str, dim: int) -> list[float]:
    """Deterministic fallback embedding when sentence-transformers is unavailable."""
    vec = [0.0] * dim
    if not text:
        return vec
    tokens = text.lower().split()
    for token in tokens:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:2], "big") % dim
        sign = 1.0 if digest[2] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@lru_cache(maxsize=1)
def _load_model():
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(SETTINGS.embedding_model)
    except Exception:
        return None


def embed_text(text: str, dim: int | None = None) -> list[float]:
    dim = dim or SETTINGS.vector_dim
    model = _load_model()
    if model is None:
        return _hash_vector(text, dim)
    vec = model.encode([text or ""], normalize_embeddings=True)[0]
    return [float(x) for x in vec]


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    n = min(len(vec_a), len(vec_b))
    dot = sum(vec_a[i] * vec_b[i] for i in range(n))
    na = math.sqrt(sum(vec_a[i] * vec_a[i] for i in range(n))) or 1.0
    nb = math.sqrt(sum(vec_b[i] * vec_b[i] for i in range(n))) or 1.0
    return dot / (na * nb)


def rank_semantic(query: str, docs: list[tuple[str, list[float] | None]]) -> list[tuple[str, float]]:
    qv = embed_text(query)
    ranked: list[tuple[str, float]] = []
    for doc_id, emb in docs:
        if emb is None:
            continue
        ranked.append((doc_id, cosine_similarity(qv, emb)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked
