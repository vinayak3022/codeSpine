from __future__ import annotations

import hashlib
import json
import math
import os
import threading
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


class _EmbeddingCache:
    """Thread-safe in-memory embedding cache backed by a JSON file.

    Replaces the previous SQLite-based cache which caused threading issues
    (database is locked / created in wrong thread) under MCP server concurrency.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] | None = None  # loaded lazily

    def _ensure_loaded(self) -> None:
        """Load cache from disk. Must be called with _lock held."""
        if self._data is not None:
            return
        if os.path.isfile(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
                    return
            except Exception:
                pass
        self._data = {}

    def _flush(self) -> None:
        """Persist cache to disk atomically. Must be called with _lock held."""
        try:
            dir_path = os.path.dirname(self._path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, separators=(",", ":"))
            os.replace(tmp, self._path)
        except Exception:
            pass

    def get(self, key: str) -> list[float] | None:
        with self._lock:
            self._ensure_loaded()
            raw = self._data.get(key)  # type: ignore[union-attr]
        if raw is None:
            return None
        try:
            return [float(x) for x in json.loads(raw)]
        except Exception:
            return None

    def set(self, key: str, vec: list[float]) -> None:
        with self._lock:
            self._ensure_loaded()
            self._data[key] = json.dumps(vec)  # type: ignore[index]
            self._flush()


_CACHE = _EmbeddingCache(SETTINGS.embedding_cache_path)


def _cache_key(text: str, dim: int) -> str:
    return hashlib.sha1(f"{SETTINGS.embedding_model}|{dim}|{text}".encode("utf-8")).hexdigest()


def embed_text(text: str, dim: int | None = None) -> list[float]:
    dim = dim or SETTINGS.vector_dim
    key = _cache_key(text or "", dim)

    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    model = _load_model()
    if model is None:
        vec = _hash_vector(text, dim)
    else:
        vec = [float(x) for x in model.encode([text or ""], normalize_embeddings=True)[0]]

    _CACHE.set(key, vec)
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
