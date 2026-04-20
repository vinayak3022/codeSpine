"""ShardedGraphStore — in-process shard coordinator.

Each project (or multi-module root) is consistently hashed to a shard index.
All modules of the same project share one shard so that cross-module graph
traversals see the full call graph without federation.

Design
------
* ``ShardedGraphStore`` maintains a pool of ``GraphStore`` instances, one per
  shard opened so far.  Shards are opened lazily on first access.
* Existing callers that receive a plain ``GraphStore`` continue to work
  unchanged.  The new entry point is ``ShardedGraphStore.shard(project_id)``
  which returns the ``GraphStore`` responsible for that project.
* Fan-out reads (``list_project_metadata``, global search) call
  ``all_shards()`` to iterate every open shard.
* ``snapshot_all()`` triggers per-shard snapshots concurrently.

Migration from v0.9.x
---------------------
If ``~/.codespine_db`` exists and the new shards directory doesn't, the
store automatically migrates the legacy DB to shard 0's path on first access
so existing indexed data isn't lost.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from typing import Any, Union

from codespine.config import SETTINGS
from codespine.db.store import GraphStore
from codespine.sharding.router import ShardRouter

# Imported lazily in _get_shard to avoid hard-dep on duckdb when not in use.
_AnyStore = Any  # GraphStore | DuckDBStore

LOGGER = logging.getLogger(__name__)


class ShardedGraphStore:
    """Coordinates multiple per-shard ``GraphStore`` instances.

    Parameters
    ----------
    read_only:
        Passed through to each ``GraphStore``.
    num_shards:
        Override for the shard count.  Defaults to ``SETTINGS.num_shards``.
    shards_dir:
        Override for the shards base directory.
    """

    def __init__(
        self,
        read_only: bool = False,
        num_shards: int | None = None,
        shards_dir: str | None = None,
        backend: str | None = None,
    ) -> None:
        self.read_only = read_only
        self.backend: str = backend or SETTINGS.backend
        self.router = ShardRouter(
            num_shards=num_shards or SETTINGS.num_shards,
            shards_dir=shards_dir or SETTINGS.shards_dir,
        )
        self._pool: dict[int, _AnyStore] = {}
        self._lock = threading.Lock()
        self._migrated = False

    # ------------------------------------------------------------------
    # Core routing
    # ------------------------------------------------------------------

    def shard(self, project_id: str) -> _AnyStore:
        """Return (or open) the store for this project's shard.

        In write mode this always returns a valid store (creating the DB if
        needed).  In read-only mode, if the shard DB has never been written to,
        this also creates it (returning an empty-but-valid store) so that
        callers can safely query it without crashing.
        """
        idx = self.router.shard_for(project_id)
        store = self._get_shard(idx)
        if store is None:
            # Fallback: create an empty writable DB so callers never crash.
            with self._lock:
                if idx not in self._pool:
                    db_path = self.router.db_path(idx)
                    snap_path = self.router.snapshot_path(idx)
                    os.makedirs(os.path.dirname(db_path), exist_ok=True)
                    self._pool[idx] = self._open_store(
                        read_only=False,  # create empty DB
                        db_path=db_path,
                        snap_path=snap_path,
                    )
                store = self._pool[idx]
        return store

    def _open_store(self, *, read_only: bool, db_path: str, snap_path: str) -> _AnyStore:
        """Instantiate the correct store class based on ``self.backend``."""
        if self.backend == "duckdb":
            from codespine.db.duckdb_store import DuckDBStore  # lazy import
            return DuckDBStore(
                read_only=read_only,
                db_path_override=db_path,
                snapshot_path_override=snap_path,
            )
        return GraphStore(
            read_only=read_only,
            db_path_override=db_path,
            snapshot_path_override=snap_path,
        )

    def _get_shard(self, idx: int) -> _AnyStore | None:
        """Return the store for shard *idx*, or None if it doesn't exist
        yet and we're in read-only mode (nothing to read there)."""
        with self._lock:
            if idx not in self._pool:
                self._maybe_migrate(idx)
                db_path = self.router.db_path(idx)
                snap_path = self.router.snapshot_path(idx)

                # In read-only mode, skip shards whose DB hasn't been created
                # yet — Kuzu refuses to create an empty DB under read_only=True.
                if self.read_only and not os.path.exists(db_path) and not os.path.exists(snap_path):
                    return None

                # Ensure parent directory exists before opening the DB.
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                self._pool[idx] = self._open_store(
                    read_only=self.read_only,
                    db_path=db_path,
                    snap_path=snap_path,
                )
            return self._pool[idx]

    def _maybe_migrate(self, idx: int) -> None:
        """One-time migration: copy legacy ~/.codespine_db → shard 0 DB path.

        Guard: only triggers when the shards_dir matches the compiled-in
        default (SETTINGS.shards_dir).  Custom / test shards_dir values are
        never eligible for migration so that test isolation is preserved.
        """
        if self._migrated or idx != 0:
            return
        self._migrated = True

        # Safety guard: never auto-migrate when using a non-default shards dir.
        # This prevents test code that passes a temp dir from accidentally
        # touching production data.
        if os.path.realpath(self.router.shards_dir) != os.path.realpath(SETTINGS.shards_dir):
            return

        legacy = SETTINGS.db_path  # ~/.codespine_db
        target = self.router.db_path(0)
        if not os.path.exists(legacy):
            return
        if os.path.exists(target):
            # Sharded layout already initialised — don't overwrite.
            return
        LOGGER.info(
            "Migrating legacy DB %s → shard 0 path %s", legacy, target
        )
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Copy first, delete after — if the copy fails the original is safe.
            if os.path.isdir(legacy):
                shutil.copytree(legacy, target)
            else:
                shutil.copy2(legacy, target)
            shutil.rmtree(legacy, ignore_errors=True)
            # Also migrate read replica if present.
            legacy_snap = SETTINGS.db_snapshot_path
            target_snap = self.router.snapshot_path(0)
            if os.path.exists(legacy_snap) and not os.path.exists(target_snap):
                if os.path.isdir(legacy_snap):
                    shutil.copytree(legacy_snap, target_snap)
                else:
                    shutil.copy2(legacy_snap, target_snap)
                shutil.rmtree(legacy_snap, ignore_errors=True)
        except OSError as exc:
            LOGGER.warning("Migration from legacy path failed: %s — starting fresh", exc)

    def all_shards(self) -> list[GraphStore]:
        """Open and return all physical shards that exist (for fan-out reads).

        Non-existent shards are skipped in read-only mode to avoid Kuzu
        errors when opening empty paths under read_only=True.
        """
        stores = []
        for i in self.router.all_shards():
            s = self._get_shard(i)
            if s is not None:
                stores.append(s)
        return stores

    def open_shards(self) -> list[GraphStore]:
        """Return only shards that have already been opened (no I/O)."""
        with self._lock:
            return [s for s in self._pool.values() if s is not None]

    # ------------------------------------------------------------------
    # Delegated project-scoped operations
    # ------------------------------------------------------------------

    def upsert_project(self, project_id: str, path: str) -> None:
        self.shard(project_id).upsert_project(project_id, path)

    def clear_project(self, project_id: str) -> None:
        self.shard(project_id).clear_project(project_id)

    def get_project_metadata(self, project_id: str) -> dict[str, Any] | None:
        return self.shard(project_id).get_project_metadata(project_id)

    def set_project_overlay_dirty(self, project_id: str, dirty: bool) -> None:
        self.shard(project_id).set_project_overlay_dirty(project_id, dirty)

    def set_project_indexed_commit(self, project_id: str, commit: str) -> None:
        self.shard(project_id).set_project_indexed_commit(project_id, commit)

    def project_file_hashes(self, project_id: str) -> dict[str, dict[str, str]]:
        return self.shard(project_id).project_file_hashes(project_id)

    def project_has_embeddings(self, project_id: str) -> bool:
        return self.shard(project_id).project_has_embeddings(project_id)

    def upsert_file_from_entry(self, entry: dict, project_path: str) -> None:
        project_id = entry.get("project_id", "")
        self.shard(project_id).upsert_file_from_entry(entry, project_path)

    def clear_file_by_path(self, project_id: str, project_path: str, file_path: str) -> None:
        self.shard(project_id).clear_file_by_path(project_id, project_path, file_path)

    # ------------------------------------------------------------------
    # Fan-out global reads
    # ------------------------------------------------------------------

    def list_project_metadata(self) -> list[dict[str, Any]]:
        """Aggregate project list across all shards."""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for store in self.all_shards():
            for rec in store.list_project_metadata():
                pid = rec.get("id", "")
                if pid and pid not in seen:
                    seen.add(pid)
                    results.append(rec)
        results.sort(key=lambda r: r.get("id", ""))
        return results

    def query_records(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a Cypher query.

        If ``project_id`` is given, the query runs only on that project's
        shard (fast path).  Otherwise the query fans out to all shards and
        results are concatenated.

        Note: fan-out only makes sense for queries whose results are
        independent per shard (e.g. listing nodes).  Queries that aggregate
        across shards (e.g. global COUNT) will return per-shard subtotals.
        """
        if project_id:
            return self.shard(project_id).query_records(query, params)
        merged: list[dict[str, Any]] = []
        for store in self.all_shards():
            merged.extend(store.query_records(query, params))
        return merged

    # ------------------------------------------------------------------
    # Snapshot all shards
    # ------------------------------------------------------------------

    def snapshot_all(self, background: bool = False) -> None:
        """Snapshot every open shard.

        In background mode all snapshots run concurrently in daemon threads
        (one per shard) rather than sequentially.
        """
        for store in self.open_shards():
            store.snapshot_to_read_replica(background=background)

    # ------------------------------------------------------------------
    # Global reset / status
    # ------------------------------------------------------------------

    def force_delete_all_data(self) -> list[str]:
        """Delete all shards' data files.  Equivalent to force_delete per shard."""
        removed: list[str] = []
        for store in self.all_shards():
            removed.extend(store.force_delete_all_data())
        return removed

    def clear_analysis_artifacts(self) -> None:
        """Fan-out: clear analysis artifacts (communities, flows, dead code) on every shard."""
        for store in self.all_shards():
            try:
                store.clear_analysis_artifacts()
            except Exception as exc:
                LOGGER.warning("clear_analysis_artifacts failed on shard: %s", exc)

    def rebuild_empty_db(self) -> None:
        """Fan-out: rebuild each shard as an empty database."""
        for store in self.all_shards():
            try:
                store.rebuild_empty_db()
            except Exception as exc:
                LOGGER.warning("rebuild_empty_db failed on shard: %s", exc)

    def snapshot_to_read_replica(self, background: bool = False) -> bool:
        """Alias for ``snapshot_all`` — matches GraphStore's API."""
        self.snapshot_all(background=background)
        return True

    def describe(self) -> dict:
        """Return a human-readable description of the shard topology."""
        shard_info = []
        for idx in self.router.all_shards():
            db_path = self.router.db_path(idx)
            shard_info.append({
                "index": idx,
                "db_path": db_path,
                "exists": os.path.exists(db_path),
                "open": idx in self._pool,
            })
        return {
            **self.router.describe(),
            "shards": shard_info,
        }

    # ------------------------------------------------------------------
    # Backward-compat helpers — delegate to shard 0 for single-project use
    # ------------------------------------------------------------------

    @property
    def overlay_store(self):
        """Expose the overlay store from shard 0 for backward compat."""
        return self._get_shard(0).overlay_store

    @staticmethod
    def stable_id(*parts: str) -> str:
        """Stable SHA1-based identifier (shard-independent)."""
        import hashlib
        raw = "::".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
