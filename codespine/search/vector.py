from __future__ import annotations

import hashlib
import math
import sqlite3
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


@lru_cache(maxsize=1)
def _embedding_cache_conn():
    conn = sqlite3.connect(SETTINGS.embedding_cache_db)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_cache (
            cache_key TEXT PRIMARY KEY,
            dim INTEGER NOT NULL,
            vector_json TEXT NOT NULL
        )
        """
    )
    return conn


def _cache_key(text: str, dim: int) -> str:
    return hashlib.sha1(f"{SETTINGS.embedding_model}|{dim}|{text}".encode("utf-8")).hexdigest()


def _get_cached_embedding(text: str, dim: int) -> list[float] | None:
    key = _cache_key(text, dim)
    conn = _embedding_cache_conn()
    row = conn.execute("SELECT vector_json FROM embedding_cache WHERE cache_key = ? AND dim = ?", (key, dim)).fetchone()
    if not row:
        return None
    import json

    return [float(x) for x in json.loads(row[0])]


def _set_cached_embedding(text: str, dim: int, vec: list[float]) -> None:
    key = _cache_key(text, dim)
    conn = _embedding_cache_conn()
    import json

    conn.execute(
        "INSERT OR REPLACE INTO embedding_cache(cache_key, dim, vector_json) VALUES (?, ?, ?)",
        (key, dim, json.dumps(vec)),
    )
    conn.commit()


def embed_text(text: str, dim: int | None = None) -> list[float]:
    dim = dim or SETTINGS.vector_dim
    cached = _get_cached_embedding(text or "", dim)
    if cached is not None:
        return cached

    model = _load_model()
    if model is None:
        vec = _hash_vector(text, dim)
        _set_cached_embedding(text or "", dim, vec)
        return vec

    vec = [float(x) for x in model.encode([text or ""], normalize_embeddings=True)[0]]
    _set_cached_embedding(text or "", dim, vec)
    return vec


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
