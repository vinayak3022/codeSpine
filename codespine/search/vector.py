from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from functools import lru_cache

from codespine.config import SETTINGS

LOGGER = logging.getLogger(__name__)


def _hash_vector(text: str, dim: int) -> list[float]:
    """Deterministic fallback embedding when sentence-transformers is unavailable.

    Uses character n-grams (bi-, tri-, and quad-grams) plus full word tokens for
    significantly better score calibration compared to word-only hashing.  Similar
    identifiers (e.g. ``getUserById`` vs ``getUserByName``) now land closer in
    vector space than they would with whole-word tokens alone.
    """
    vec = [0.0] * dim
    if not text:
        return vec
    normalized = text.lower()

    features: list[str] = []
    # Include whole words (split on camelCase boundaries too)
    import re as _re
    words = _re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower().split()
    features.extend(words)
    # Character n-grams (bigrams, trigrams, quadgrams) over normalized text
    for n in (2, 3, 4):
        for i in range(len(normalized) - n + 1):
            features.append(normalized[i : i + n])

    for feat in features:
        digest = hashlib.sha1(feat.encode("utf-8")).digest()
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
        # Delete the old SQLite cache file left by versions < 0.4.0.
        old_sqlite = self._path.replace(".json", ".sqlite3")
        if os.path.isfile(old_sqlite):
            try:
                os.remove(old_sqlite)
            except OSError:
                pass
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

    def clear(self) -> None:
        """Wipe the in-memory cache and delete the backing file."""
        with self._lock:
            self._data = {}
            try:
                os.remove(self._path)
            except OSError:
                pass

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
    return embed_texts([text], dim=dim)[0]


def embed_texts(texts: list[str], dim: int | None = None) -> list[list[float]]:
    dim = dim or SETTINGS.vector_dim
    if not texts:
        return []

    normalized_texts = [text or "" for text in texts]
    unique_texts = list(dict.fromkeys(normalized_texts))
    vectors_by_text: dict[str, list[float]] = {}
    missing_texts: list[str] = []

    for text in unique_texts:
        cached = _CACHE.get(_cache_key(text, dim))
        if cached is None:
            missing_texts.append(text)
        else:
            vectors_by_text[text] = cached

    if missing_texts:
        model = _load_model()
        if model is None:
            missing_vectors = [_hash_vector(text, dim) for text in missing_texts]
        else:
            missing_vectors = [
                [float(x) for x in vec]
                for vec in model.encode(missing_texts, normalize_embeddings=True)
            ]
        for text, vec in zip(missing_texts, missing_vectors):
            _CACHE.set(_cache_key(text, dim), vec)
            vectors_by_text[text] = vec

    return [vectors_by_text[text] for text in normalized_texts]


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    n = min(len(vec_a), len(vec_b))
    dot = sum(vec_a[i] * vec_b[i] for i in range(n))
    na = math.sqrt(sum(vec_a[i] * vec_a[i] for i in range(n))) or 1.0
    nb = math.sqrt(sum(vec_b[i] * vec_b[i] for i in range(n))) or 1.0
    return dot / (na * nb)


def _search_query_text(query: str) -> str:
    """Build structured query text for semantic search.

    Produces text in the same format as the index-time embedding templates
    in engine.py, so that query vectors align with document vectors.

    Identifier-like queries (no spaces, camelCase) are treated as symbol
    lookups; free-text queries are passed through with a generic prefix.
    """
    if " " not in query.strip():
        return (
            f"type: symbol, name: {query.strip()}, "
            f"qualified name: {query.strip()}"
        )
    return f"query: {query.strip()}"


def rank_semantic(query: str, docs: list[tuple[str, list[float] | None]]) -> list[tuple[str, float]]:
    qv = embed_text(_search_query_text(query))
    ranked: list[tuple[str, float]] = []
    for doc_id, emb in docs:
        if emb is None:
            continue
        ranked.append((doc_id, cosine_similarity(qv, emb)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def rank_semantic_sql(
    store,
    query: str,
    *,
    pool_size: int = 5000,
) -> list[tuple[str, float]] | None:
    """Rank symbols by DuckDB SQL-native cosine similarity.

    Pushes vector distance computation to the SQL engine instead of loading
    all embeddings into Python.  Returns ``None`` when the SQL path is not
    supported (older DuckDB, Kuzu backend, empty index) — callers should
    fall back to Python ``rank_semantic()`` in that case.

    Returns ``[(doc_id, score), ...]`` sorted descending, limited to *pool_size*,
    or ``None`` if the SQL path is unavailable.
    """
    qv = embed_text(_search_query_text(query))
    if not qv:
        return None
    dim = SETTINGS.vector_dim

    # Try DuckDB SQL-native path via list_cosine_similarity.
    try:
        rows = store.query_records(
            f"""
            SELECT s.id,
                   list_cosine_similarity(s.embedding, $qv::FLOAT[{dim}]) AS score
            FROM symbols s
            WHERE s.embedding IS NOT NULL
            ORDER BY score DESC
            LIMIT $limit
            """,
            {"qv": qv, "limit": pool_size},
        )
        if rows:
            return [(row["id"], float(row["score"])) for row in rows]
    except Exception:
        pass

    # Fallback: try array_distance (negated for higher=better).
    try:
        rows = store.query_records(
            f"""
            SELECT s.id,
                   -array_distance(s.embedding, $qv::FLOAT[{dim}]) AS score
            FROM symbols s
            WHERE s.embedding IS NOT NULL
            ORDER BY score DESC
            LIMIT $limit
            """,
            {"qv": qv, "limit": pool_size},
        )
        if rows:
            return [(row["id"], float(row["score"])) for row in rows]
    except Exception:
        pass

    # SQL path not available — caller will fall back to Python rank_semantic.
    return None


# ---------------------------------------------------------------------------
# Cross-encoder reranker (optional, opt-in via config)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_reranker():
    """Load the cross-encoder reranker model (or None if not configured/available)."""
    model_name = SETTINGS.cross_encoder_model
    if not model_name:
        return None
    try:
        from sentence_transformers import CrossEncoder

        return CrossEncoder(model_name)
    except Exception:
        LOGGER.warning("Cross-encoder model '%s' not available", model_name)
        return None


def rank_cross_encoder(
    query: str,
    candidates: list[tuple[str, str]],
) -> list[tuple[str, float]]:
    """Re-rank candidate (doc_id, text) pairs using a cross-encoder.

    Only applied to the top-N candidates from the RRF pool to keep latency
    reasonable (cross-encoders are O(n) per query-doc pair).
    Returns (doc_id, score) sorted descending.
    """
    reranker = _load_reranker()
    if reranker is None or not candidates:
        return [(doc_id, 1.0) for doc_id, _ in candidates]

    pairs = [(query, text) for _, text in candidates]
    try:
        scores = reranker.predict(pairs, show_progress_bar=False)
    except Exception as exc:
        LOGGER.warning("Cross-encoder reranking failed: %s", exc)
        return [(doc_id, 1.0) for doc_id, _ in candidates]

    ranked: list[tuple[str, float]] = []
    for (doc_id, _), score in zip(candidates, scores):
        ranked.append((doc_id, float(score)))
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked
