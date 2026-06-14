"""LRU result cache for CodeSpine MCP tools.

Avoids recomputing expensive analyses (impact BFS, dead-code scan, community
lookup) when the same arguments are passed and the underlying index hasn't
changed since the last call.

Cache key: ``(tool_name, args_hash, snapshot_mtime_ns)``
  - ``tool_name`` — the MCP tool that produced the result
  - ``args_hash`` — SHA-1 of the JSON-serialised arguments (sorted keys)
   - ``snapshot_mtime_ns`` — read-replica mtime at nanosecond precision, so a
     new snapshot invalidates all cached results for the affected store

TTL: entries are evicted after ``ttl_s`` seconds (default 300 s / 5 min) even
if the cache isn't full, preventing stale results across long sessions.

Usage
-----
    from codespine.cache.result_cache import ResultCache

    _cache = ResultCache(maxsize=256, ttl_s=300.0)

    key = _cache.make_key("get_impact", {"symbol": "Foo", "project": "myapp"}, mtime)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    result = expensive_computation(...)
    _cache.put(key, result)
    return result
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any


class ResultCache:
    """Thread-safe LRU cache for pre-serialised JSON tool results.

    Parameters
    ----------
    maxsize:
        Maximum number of entries to keep.  Oldest entry is evicted when
        the cache is full (LRU eviction).
    ttl_s:
        Time-to-live in seconds.  Entries older than this are treated as
        missing even if they're still in the cache.
    """

    def __init__(self, maxsize: int = 256, ttl_s: float = 300.0) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_s
        # OrderedDict preserves insertion order: oldest → newest
        self._cache: OrderedDict[tuple, tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Key construction
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(
        tool_name: str,
        args: dict[str, Any],
        snapshot_mtime: float,
    ) -> tuple:
        """Build a cache key from tool name, arguments, and index timestamp.

        Parameters
        ----------
        tool_name:
            Name of the MCP tool (e.g. ``"get_impact"``).
        args:
            Tool arguments dict (``None`` values included so missing optional
            args don't collide with explicitly-set ones).
        snapshot_mtime:
            Last-modified time of the read-replica sentinel file.  The key
            preserves nanosecond precision so rapid snapshots do not collide.
        """
        try:
            args_bytes = json.dumps(args, sort_keys=True, default=str).encode()
        except Exception:
            args_bytes = str(args).encode()
        args_hash = hashlib.sha1(args_bytes).hexdigest()[:16]
        return (tool_name, args_hash, int(round(snapshot_mtime * 1_000_000_000)))

    # ------------------------------------------------------------------
    # Cache operations
    # ------------------------------------------------------------------

    def get(self, key: tuple) -> str | None:
        """Return the cached value for *key*, or ``None`` if missing/expired."""
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            value, inserted_at = self._cache[key]
            if time.monotonic() - inserted_at > self._ttl:
                del self._cache[key]
                self._misses += 1
                return None
            # Promote to most-recently-used position.
            self._cache.move_to_end(key)
            self._hits += 1
            return value

    def put(self, key: tuple, value: str) -> None:
        """Store *value* under *key*.  Evicts LRU entry if cache is full."""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (value, time.monotonic())
            # Evict oldest entries until we're within maxsize.
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate(self) -> int:
        """Clear the entire cache.  Call after any index mutation.

        Returns the number of entries evicted.
        """
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
            return n

    def invalidate_tool(self, tool_name: str) -> int:
        """Evict all entries for a specific tool.

        Returns the number of entries removed.
        """
        with self._lock:
            keys_to_remove = [k for k in self._cache if k[0] == tool_name]
            for k in keys_to_remove:
                del self._cache[k]
            return len(keys_to_remove)

    # ------------------------------------------------------------------
    # Stats / introspection
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Return cache statistics (size, hit/miss counts, hit rate)."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "maxsize": self._maxsize,
                "ttl_s": self._ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 3) if total else 0.0,
            }

    def __repr__(self) -> str:  # pragma: no cover
        s = self.stats()
        return (
            f"ResultCache(size={s['size']}/{s['maxsize']}, "
            f"hits={s['hits']}, misses={s['misses']}, "
            f"hit_rate={s['hit_rate']:.1%})"
        )
