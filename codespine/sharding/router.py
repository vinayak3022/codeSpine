"""Consistent-hash shard router for CodeSpine.

Design
------
* ``num_shards`` physical shards — each shard owns an independent KùzuDB at
  ``~/.codespine/shards/{N}/db``.
* Shard key = *root project name* (the part before ``::`` in a multi-module
  project ID).  This guarantees that all modules of the same project are
  co-located in the same shard so that cross-module call resolution still
  works in one graph traversal.
* Virtual-node ring (``VIRTUAL_NODES_PER_SHARD = 150``) gives an even
  distribution even for small shard counts.
* ``num_shards`` can be changed at any time; affected projects must be
  re-indexed, but unaffected projects continue to work.

Env var override
----------------
``CODESPINE_SHARDS=N`` (integer, default 4) sets the number of shards at
process start.  0 or 1 disables sharding (all projects land in shard 0).
"""

from __future__ import annotations

import bisect
import hashlib
import os

VIRTUAL_NODES_PER_SHARD = 150  # virtual ring entries per physical shard


class ShardRouter:
    """Maps project IDs to shard indices via a consistent-hash ring.

    Parameters
    ----------
    num_shards:
        Number of physical shards.  Defaults to the ``CODESPINE_SHARDS``
        environment variable, or ``4`` if unset.
    shards_dir:
        Base directory that holds per-shard sub-directories.
    """

    def __init__(
        self,
        num_shards: int | None = None,
        shards_dir: str | None = None,
    ) -> None:
        _env = os.environ.get("CODESPINE_SHARDS", "").strip()
        _default = max(1, int(_env)) if _env.isdigit() else 4
        self.num_shards: int = max(1, num_shards if num_shards is not None else _default)
        self.shards_dir: str = shards_dir or os.path.expanduser("~/.codespine/shards")

        # Build virtual-node ring: list of (ring_point, shard_index) sorted by ring_point
        self._ring: list[tuple[int, int]] = []
        for shard_idx in range(self.num_shards):
            for vn in range(VIRTUAL_NODES_PER_SHARD):
                point = self._hash_key(f"shard-{shard_idx}-vn-{vn}")
                self._ring.append((point, shard_idx))
        self._ring.sort()
        self._ring_points = [p for p, _ in self._ring]

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_key(key: str) -> int:
        """Deterministic 64-bit hash of a string."""
        raw = hashlib.md5(key.encode("utf-8")).digest()
        # Use first 8 bytes as unsigned 64-bit integer for wide ring range.
        return int.from_bytes(raw[:8], "big")

    def _root_key(self, project_id: str) -> str:
        """Extract the root portion of a project_id for co-location.

        For multi-module projects (format ``root::module``), all modules of
        the same root must land on the same shard so that cross-module graph
        traversals work without federation.
        """
        return project_id.split("::")[0] if "::" in project_id else project_id

    def shard_for(self, project_id: str) -> int:
        """Return the shard index [0, num_shards) for the given project_id."""
        if self.num_shards == 1:
            return 0
        point = self._hash_key(self._root_key(project_id))
        pos = bisect.bisect_left(self._ring_points, point)
        # Wrap around the ring
        _, shard_idx = self._ring[pos % len(self._ring)]
        return shard_idx

    def all_shards(self) -> list[int]:
        """Return all shard indices."""
        return list(range(self.num_shards))

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def db_path(self, shard_index: int) -> str:
        """Absolute write-DB path for a shard."""
        return os.path.join(self.shards_dir, str(shard_index), "db")

    def snapshot_path(self, shard_index: int) -> str:
        """Absolute read-replica path for a shard."""
        return os.path.join(self.shards_dir, str(shard_index), "db_read")

    def shard_home(self, shard_index: int) -> str:
        """Directory that holds all data for a shard."""
        return os.path.join(self.shards_dir, str(shard_index))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def describe(self) -> dict:
        """Return a human-readable summary of the routing table."""
        return {
            "num_shards": self.num_shards,
            "shards_dir": self.shards_dir,
            "virtual_nodes_per_shard": VIRTUAL_NODES_PER_SHARD,
            "ring_size": len(self._ring),
        }
