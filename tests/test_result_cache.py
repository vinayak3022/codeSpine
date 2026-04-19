"""Tests for codespine.cache.result_cache.ResultCache."""

from __future__ import annotations

import time

import pytest

from codespine.cache.result_cache import ResultCache


# ---------------------------------------------------------------------------
# make_key
# ---------------------------------------------------------------------------


def test_make_key_same_args_same_key():
    k1 = ResultCache.make_key("get_impact", {"symbol": "Foo", "project": None}, 1000.0)
    k2 = ResultCache.make_key("get_impact", {"symbol": "Foo", "project": None}, 1000.0)
    assert k1 == k2


def test_make_key_different_tool():
    k1 = ResultCache.make_key("get_impact", {"symbol": "Foo"}, 1000.0)
    k2 = ResultCache.make_key("detect_dead_code", {"symbol": "Foo"}, 1000.0)
    assert k1 != k2


def test_make_key_different_mtime():
    k1 = ResultCache.make_key("get_impact", {"symbol": "Foo"}, 1000.0)
    k2 = ResultCache.make_key("get_impact", {"symbol": "Foo"}, 1001.0)
    assert k1 != k2


def test_make_key_different_args():
    k1 = ResultCache.make_key("search", {"query": "Foo"}, 1000.0)
    k2 = ResultCache.make_key("search", {"query": "Bar"}, 1000.0)
    assert k1 != k2


def test_make_key_arg_order_independent():
    k1 = ResultCache.make_key("t", {"a": 1, "b": 2}, 0.0)
    k2 = ResultCache.make_key("t", {"b": 2, "a": 1}, 0.0)
    assert k1 == k2  # sorted keys in json.dumps


# ---------------------------------------------------------------------------
# get / put / hit
# ---------------------------------------------------------------------------


def test_cache_miss_returns_none():
    c = ResultCache()
    assert c.get(("x", "y", 0.0)) is None


def test_cache_put_and_get():
    c = ResultCache()
    k = ResultCache.make_key("t", {}, 0.0)
    c.put(k, '{"result": 1}')
    assert c.get(k) == '{"result": 1}'


def test_cache_hit_stat():
    c = ResultCache()
    k = ResultCache.make_key("t", {}, 0.0)
    c.put(k, "v")
    c.get(k)
    s = c.stats()
    assert s["hits"] == 1
    assert s["misses"] == 0


def test_cache_miss_stat():
    c = ResultCache()
    c.get(("x", "y", 0.0))
    s = c.stats()
    assert s["misses"] == 1
    assert s["hits"] == 0


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_lru_eviction():
    c = ResultCache(maxsize=3)
    for i in range(4):
        k = ResultCache.make_key("t", {"i": i}, 0.0)
        c.put(k, str(i))
    # First entry should have been evicted.
    k0 = ResultCache.make_key("t", {"i": 0}, 0.0)
    assert c.get(k0) is None
    # Later entries should still be present.
    k3 = ResultCache.make_key("t", {"i": 3}, 0.0)
    assert c.get(k3) == "3"


def test_lru_promotes_on_get():
    c = ResultCache(maxsize=3)
    keys = [ResultCache.make_key("t", {"i": i}, 0.0) for i in range(3)]
    for i, k in enumerate(keys):
        c.put(k, str(i))
    # Access key 0 to promote it to MRU.
    c.get(keys[0])
    # Add a new entry — key 1 (now oldest) should be evicted.
    k3 = ResultCache.make_key("t", {"i": 3}, 0.0)
    c.put(k3, "3")
    assert c.get(keys[0]) is not None  # was promoted, should survive
    assert c.get(keys[1]) is None      # was LRU, should be evicted


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_ttl_expiry(monkeypatch):
    c = ResultCache(ttl_s=0.05)
    k = ResultCache.make_key("t", {}, 0.0)
    c.put(k, "v")
    # Patch monotonic to advance time past TTL.
    real_monotonic = time.monotonic

    class _FakeClock:
        def __init__(self):
            self._base = real_monotonic()

        def __call__(self):
            return self._base + 1.0  # 1 second ahead

    monkeypatch.setattr(time, "monotonic", _FakeClock())
    assert c.get(k) is None


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------


def test_invalidate_clears_all():
    c = ResultCache()
    for i in range(5):
        k = ResultCache.make_key("t", {"i": i}, 0.0)
        c.put(k, str(i))
    removed = c.invalidate()
    assert removed == 5
    assert c.stats()["size"] == 0


def test_invalidate_tool():
    c = ResultCache()
    k1 = ResultCache.make_key("get_impact", {"s": "A"}, 0.0)
    k2 = ResultCache.make_key("detect_dead_code", {}, 0.0)
    c.put(k1, "a")
    c.put(k2, "b")
    removed = c.invalidate_tool("get_impact")
    assert removed == 1
    assert c.get(k1) is None
    assert c.get(k2) == "b"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_hit_rate():
    c = ResultCache()
    k = ResultCache.make_key("t", {}, 0.0)
    c.put(k, "v")
    c.get(k)       # hit
    c.get(k)       # hit
    c.get(("x",))  # miss
    s = c.stats()
    assert s["hits"] == 2
    assert s["misses"] == 1
    assert abs(s["hit_rate"] - 2 / 3) < 0.01
