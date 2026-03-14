from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import kuzu

from codespine.config import SETTINGS
from codespine.db.schema import ensure_schema

LOGGER = logging.getLogger(__name__)

_WRITE_BUFFER_POOL_SIZE = 512 * 1024 * 1024  # 512 MB – room for large community detection
_READ_BUFFER_POOL_SIZE = 128 * 1024 * 1024   # 128 MB – point queries only; keep footprint small
_RECOVERABLE_DB_ERROR_MARKERS = (
    "storage version mismatch",
    "catalog version mismatch",
    "database version is not supported",
    "wal version mismatch",
    "corrupt",
    "corrupted",
    "invalid database",
)


@dataclass
class GraphStore:
    read_only: bool = False

    def __post_init__(self) -> None:
        self._tls: threading.local = threading.local()
        from codespine.overlay.store import OverlayStore

        self.overlay_store = OverlayStore()

        # Read-only callers (MCP, CLI reads) use the read replica when available.
        # This isolates them from the write process's buffer pool and WAL churn.
        if self.read_only and os.path.exists(SETTINGS.db_snapshot_path):
            db_path = SETTINGS.db_snapshot_path
        else:
            db_path = SETTINGS.db_path

        try:
            self.db = self._open_with_recovery(db_path)
        except Exception as exc:
            fallback = os.path.join("/tmp", ".codespine_db")
            LOGGER.warning("Primary DB path failed (%s). Falling back to %s", exc, fallback)
            self.db = self._open_with_recovery(fallback)
        if not self.read_only:
            self._ensure_schema_with_recovery()

    def _open_db(self, path: str) -> kuzu.Database:
        pool = _READ_BUFFER_POOL_SIZE if self.read_only else _WRITE_BUFFER_POOL_SIZE
        # Newer Kuzu versions accept read_only; fall back for older ones.
        try:
            return kuzu.Database(path, buffer_pool_size=pool, read_only=self.read_only)
        except TypeError:
            return kuzu.Database(path, buffer_pool_size=pool)

    @staticmethod
    def _is_recoverable_db_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _RECOVERABLE_DB_ERROR_MARKERS)

    @staticmethod
    def _remove_db_path(path: str) -> None:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)

    def _open_with_recovery(self, path: str) -> kuzu.Database:
        try:
            return self._open_db(path)
        except Exception as exc:
            if not self._is_recoverable_db_error(exc):
                raise
            LOGGER.warning("Removing corrupted or incompatible Kuzu DB at %s: %s", path, exc)
            self._remove_db_path(path)
            self._tls = threading.local()
            return self._open_db(path)

    def _ensure_schema_with_recovery(self) -> None:
        try:
            ensure_schema(self._conn())
        except Exception as exc:
            path = getattr(self.db, "database_path", SETTINGS.db_path)
            if not self._is_recoverable_db_error(exc):
                raise
            LOGGER.warning("Rebuilding corrupted or incompatible Kuzu DB at %s during schema init: %s", path, exc)
            self._remove_db_path(path)
            self.db = self._open_db(path)
            self._tls = threading.local()
            ensure_schema(self._conn())

    def _conn(self) -> kuzu.Connection:
        """Return the per-thread Kuzu connection, creating it lazily."""
        if not hasattr(self._tls, "conn") or self._tls.conn is None:
            self._tls.conn = kuzu.Connection(self.db)
        return self._tls.conn

    @staticmethod
    def stable_id(*parts: str) -> str:
        raw = "::".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def execute(self, query: str, params: dict[str, Any] | None = None):
        return self._conn().execute(query, params or {})

    @contextmanager
    def transaction(self):
        tx_started = True
        try:
            self.execute("BEGIN TRANSACTION")
        except Exception:
            tx_started = False
        try:
            yield
            if tx_started:
                try:
                    self.execute("COMMIT")
                except Exception as exc:
                    if "No active transaction" not in str(exc):
                        raise
        except Exception:
            if tx_started:
                try:
                    self.execute("ROLLBACK")
                except Exception:
                    # Kuzu may have already rolled back (e.g. on OOM), making a
                    # second ROLLBACK raise "No active transaction". Swallow it.
                    pass
            raise

    def clear_project(self, project_id: str) -> None:
        file_recs = self.query_records("MATCH (f:File) WHERE f.project_id = $pid RETURN f.id as id", {"pid": project_id})
        for idx, rec in enumerate(file_recs, start=1):
            self.clear_file(rec["id"])
            if idx % 50 == 0:
                self._recycle_conn()
        self.execute("MATCH (p:Project) WHERE p.id = $pid DETACH DELETE p", {"pid": project_id})
        self._recycle_conn()

    def upsert_project(self, project_id: str, path: str) -> None:
        self.execute(
            """
            MERGE (p:Project {id: $id})
            SET p.path = $path,
                p.language = 'java',
                p.indexed_at = $ts,
                p.indexed_commit = coalesce(p.indexed_commit, ''),
                p.overlay_dirty = coalesce(p.overlay_dirty, false)
            """,
            {"id": project_id, "path": path, "ts": str(int(time.time()))},
        )

    def set_project_overlay_dirty(self, project_id: str, dirty: bool) -> None:
        self.execute(
            "MATCH (p:Project {id: $id}) SET p.overlay_dirty = $dirty",
            {"id": project_id, "dirty": bool(dirty)},
        )

    def set_project_indexed_commit(self, project_id: str, commit: str) -> None:
        self.execute(
            """
            MATCH (p:Project {id: $id})
            SET p.indexed_commit = $commit,
                p.indexed_at = $ts
            """,
            {"id": project_id, "commit": commit, "ts": str(int(time.time()))},
        )

    def get_project_metadata(self, project_id: str) -> dict[str, Any] | None:
        recs = self.query_records(
            """
            MATCH (p:Project)
            WHERE p.id = $pid
            RETURN p.id as id,
                   p.path as path,
                   p.language as language,
                   p.indexed_at as indexed_at,
                   p.indexed_commit as indexed_commit,
                   p.overlay_dirty as overlay_dirty
            LIMIT 1
            """,
            {"pid": project_id},
        )
        return recs[0] if recs else None

    def list_project_metadata(self) -> list[dict[str, Any]]:
        return self.query_records(
            """
            MATCH (p:Project)
            RETURN p.id as id,
                   p.path as path,
                   p.language as language,
                   p.indexed_at as indexed_at,
                   p.indexed_commit as indexed_commit,
                   p.overlay_dirty as overlay_dirty
            ORDER BY p.id
            """
        )

    def project_has_embeddings(self, project_id: str) -> bool:
        recs = self.query_records(
            """
            MATCH (s:Symbol), (f:File)
            WHERE s.file_id = f.id
              AND f.project_id = $pid
              AND s.embedding IS NOT NULL
            RETURN count(s) as count
            """,
            {"pid": project_id},
        )
        return bool(recs and int(recs[0].get("count") or 0) > 0)

    def project_file_hashes(self, project_id: str) -> dict[str, dict[str, str]]:
        recs = self.query_records(
            """
            MATCH (f:File)
            WHERE f.project_id = $pid
            RETURN f.id as id, f.path as path, f.hash as hash
            """,
            {"pid": project_id},
        )
        return {r["id"]: {"path": r.get("path", ""), "hash": r.get("hash", "")} for r in recs}

    def clear_file(self, file_id: str) -> None:
        self.execute(
            """
            MATCH (s:Symbol) WHERE s.file_id = $fid
            DETACH DELETE s
            """,
            {"fid": file_id},
        )
        self.execute(
            """
            MATCH (m:Method), (c:Class)
            WHERE m.class_id = c.id AND c.file_id = $fid
            DETACH DELETE m
            """,
            {"fid": file_id},
        )
        self.execute(
            """
            MATCH (c:Class) WHERE c.file_id = $fid
            DETACH DELETE c
            """,
            {"fid": file_id},
        )
        self.execute(
            """
            MATCH (f:File {id: $fid})
            DETACH DELETE f
            """,
            {"fid": file_id},
        )

    def list_methods(self) -> list[dict[str, Any]]:
        return self.query_records(
            """
            MATCH (m:Method), (c:Class)
            WHERE m.class_id = c.id
            RETURN m.id as method_id, m.name as name, m.signature as signature, c.fqcn as class_fqcn
            """
        )

    def upsert_file(self, file_id: str, path: str, project_id: str, is_test: bool, digest: str) -> None:
        self.execute(
            """
            MERGE (f:File {id: $id})
            SET f.path = $path, f.project_id = $project_id, f.is_test = $is_test, f.hash = $hash
            """,
            {
                "id": file_id,
                "path": path,
                "project_id": project_id,
                "is_test": is_test,
                "hash": digest,
            },
        )

    def upsert_files_batch(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.upsert_file(
                file_id=record["id"],
                path=record["path"],
                project_id=record["project_id"],
                is_test=bool(record["is_test"]),
                digest=record["hash"],
            )

    def upsert_class(self, class_id: str, fqcn: str, name: str, package: str, file_id: str) -> None:
        self.execute(
            """
            MERGE (c:Class {id: $id})
            SET c.fqcn = $fqcn, c.name = $name, c.package = $package, c.file_id = $file_id
            """,
            {
                "id": class_id,
                "fqcn": fqcn,
                "name": name,
                "package": package,
                "file_id": file_id,
            },
        )

    def upsert_classes_batch(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.upsert_class(
                class_id=record["id"],
                fqcn=record["fqcn"],
                name=record["name"],
                package=record["package"],
                file_id=record["file_id"],
            )

    def upsert_method(
        self,
        method_id: str,
        class_id: str,
        name: str,
        signature: str,
        return_type: str,
        modifiers: list[str],
        is_constructor: bool,
        is_test: bool,
    ) -> None:
        self.execute(
            """
            MERGE (m:Method {id: $id})
            SET m.class_id = $class_id,
                m.name = $name,
                m.signature = $signature,
                m.return_type = $return_type,
                m.modifiers = $modifiers,
                m.is_constructor = $is_constructor,
                m.is_test = $is_test
            """,
            {
                "id": method_id,
                "class_id": class_id,
                "name": name,
                "signature": signature,
                "return_type": return_type,
                "modifiers": modifiers,
                "is_constructor": is_constructor,
                "is_test": is_test,
            },
        )
        self.execute(
            "MATCH (c:Class {id: $cid}), (m:Method {id: $mid}) MERGE (c)-[:HAS_METHOD]->(m)",
            {"cid": class_id, "mid": method_id},
        )

    def upsert_methods_batch(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.upsert_method(
                method_id=record["id"],
                class_id=record["class_id"],
                name=record["name"],
                signature=record["signature"],
                return_type=record["return_type"],
                modifiers=record["modifiers"],
                is_constructor=bool(record["is_constructor"]),
                is_test=bool(record["is_test"]),
            )

    def upsert_symbol(
        self,
        symbol_id: str,
        kind: str,
        name: str,
        fqname: str,
        file_id: str,
        line: int,
        col: int,
        embedding: list[float] | None,
    ) -> None:
        self.execute(
            """
            MERGE (s:Symbol {id: $id})
            SET s.kind = $kind,
                s.name = $name,
                s.fqname = $fqname,
                s.file_id = $file_id,
                s.line = $line,
                s.col = $col,
                s.embedding = $embedding
            """,
            {
                "id": symbol_id,
                "kind": kind,
                "name": name,
                "fqname": fqname,
                "file_id": file_id,
                "line": line,
                "col": col,
                "embedding": embedding,
            },
        )
        self.execute(
            "MATCH (f:File {id: $fid}), (s:Symbol {id: $sid}) MERGE (f)-[:DECLARES]->(s)",
            {"fid": file_id, "sid": symbol_id},
        )

    def upsert_symbols_batch(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.upsert_symbol(
                symbol_id=record["id"],
                kind=record["kind"],
                name=record["name"],
                fqname=record["fqname"],
                file_id=record["file_id"],
                line=int(record["line"]),
                col=int(record["col"]),
                embedding=record.get("embedding"),
            )

    def add_call(self, source_id: str, target_id: str, confidence: float, reason: str) -> None:
        self.execute(
            """
            MATCH (source:Method {id: $source_id}), (target:Method {id: $target_id})
            MERGE (source)-[:CALLS {confidence: $confidence, reason: $reason}]->(target)
            """,
            {
                "source_id": source_id,
                "target_id": target_id,
                "confidence": confidence,
                "reason": reason,
            },
        )

    def add_calls_batch(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.add_call(
                source_id=record["source_id"],
                target_id=record["target_id"],
                confidence=float(record["confidence"]),
                reason=record["reason"],
            )

    def add_reference(self, rel: str, src_label: str, src_id: str, dst_label: str, dst_id: str, confidence: float) -> None:
        if rel not in {"REFERENCES_TYPE", "IMPLEMENTS", "OVERRIDES"}:
            return
        query = (
            f"MATCH (s:{src_label} {{id: $src_id}}), (d:{dst_label} {{id: $dst_id}}) "
            f"MERGE (s)-[:{rel} {{confidence: $confidence}}]->(d)"
        )
        self.execute(query, {"src_id": src_id, "dst_id": dst_id, "confidence": confidence})

    def add_references_batch(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            self.add_reference(
                rel=record["rel"],
                src_label=record["src_label"],
                src_id=record["src_id"],
                dst_label=record["dst_label"],
                dst_id=record["dst_id"],
                confidence=float(record["confidence"]),
            )

    def _recycle_conn(self) -> None:
        """Drop and recreate the per-thread connection to release buffer pages."""
        try:
            if hasattr(self._tls, "conn") and self._tls.conn is not None:
                self._tls.conn = None
        except Exception:
            pass

    def clear_communities(self) -> None:
        self.execute("MATCH ()-[r:IN_COMMUNITY]->() DELETE r")
        self._recycle_conn()
        self.execute("MATCH (c:Community) DETACH DELETE c")
        self._recycle_conn()

    def clear_flows(self) -> None:
        self.execute("MATCH ()-[r:IN_FLOW]->() DELETE r")
        self._recycle_conn()
        self.execute("MATCH (f:Flow) DETACH DELETE f")
        self._recycle_conn()

    def clear_coupling(self) -> None:
        self.execute("MATCH ()-[r:CO_CHANGED_WITH]->() DELETE r")
        self._recycle_conn()

    def clear_analysis_artifacts(self) -> None:
        self.clear_communities()
        self.clear_flows()
        self.clear_coupling()

    def rebuild_empty_db(self) -> None:
        self._recycle_conn()
        path = SETTINGS.db_path
        try:
            self._remove_db_path(path)
        except OSError:
            fallback = os.path.join("/tmp", ".codespine_db")
            self._remove_db_path(fallback)
            self.db = self._open_db(fallback)
        else:
            self.db = self._open_db(path)
        self._tls = threading.local()
        ensure_schema(self._conn())

    def set_community(self, community_id: str, label: str, cohesion: float, symbol_ids: list[str]) -> None:
        self.execute(
            "MERGE (c:Community {id: $id}) SET c.label = $label, c.cohesion = $cohesion",
            {"id": community_id, "label": label, "cohesion": cohesion},
        )
        # Commit in batches of 500 to keep Kuzu's buffer pool from OOMing on
        # large communities.  After each batch, recycle the connection so Kuzu
        # can release buffer pages accumulated during the transaction.
        _BATCH = 500
        for i in range(0, len(symbol_ids), _BATCH):
            batch = symbol_ids[i : i + _BATCH]
            with self.transaction():
                for sid in batch:
                    self.execute(
                        "MATCH (s:Symbol {id: $sid}), (c:Community {id: $cid}) MERGE (s)-[:IN_COMMUNITY]->(c)",
                        {"sid": sid, "cid": community_id},
                    )
            # Recycle connection after each batch to let Kuzu free buffer pages
            self._recycle_conn()

    def set_flow(self, flow_id: str, entry_symbol_id: str, kind: str, symbols_at_depth: list[tuple[str, int]]) -> None:
        self.execute(
            "MERGE (f:Flow {id: $id}) SET f.entry_symbol_id = $entry, f.kind = $kind",
            {"id": flow_id, "entry": entry_symbol_id, "kind": kind},
        )
        _BATCH = 500
        for i in range(0, len(symbols_at_depth), _BATCH):
            batch = symbols_at_depth[i : i + _BATCH]
            with self.transaction():
                for sid, depth in batch:
                    self.execute(
                        "MATCH (s:Symbol {id: $sid}), (f:Flow {id: $fid}) MERGE (s)-[:IN_FLOW {depth: $depth}]->(f)",
                        {"sid": sid, "fid": flow_id, "depth": int(depth)},
                    )
            self._recycle_conn()

    def upsert_coupling(self, file_a: str, file_b: str, strength: float, cochanges: int, months: int) -> None:
        self.execute(
            """
            MATCH (a:File {id: $a}), (b:File {id: $b})
            MERGE (a)-[:CO_CHANGED_WITH {strength: $strength, cochanges: $cochanges, months: $months}]->(b)
            """,
            {
                "a": file_a,
                "b": file_b,
                "strength": strength,
                "cochanges": int(cochanges),
                "months": int(months),
            },
        )

    @staticmethod
    def snapshot_to_read_replica() -> bool:
        """Atomically copy the write DB to the read-replica path.

        The read replica is used by the MCP daemon and all read-only CLI
        commands so they never contend with the write process's buffer pool.
        Returns True on success, False if the source DB does not exist.
        """
        src = SETTINGS.db_path
        dst = SETTINGS.db_snapshot_path
        if not os.path.exists(src):
            return False
        tmp = dst + ".tmp"
        try:
            if os.path.exists(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            if os.path.isdir(src):
                shutil.copytree(src, tmp)
            else:
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                shutil.copy2(src, tmp)
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            os.rename(tmp, dst)
            # Sentinel: MCP daemon watches this file's mtime to know when to reload.
            sentinel = dst + ".updated"
            with open(sentinel, "w", encoding="utf-8") as f:
                f.write(str(int(time.time())))
            return True
        except Exception as exc:
            LOGGER.warning("Snapshot to read replica failed: %s", exc)
            if os.path.exists(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
            return False

    def query_records(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        frame = self.execute(query, params or {}).get_as_df()
        if frame.empty:
            return []
        return json.loads(frame.to_json(orient="records"))
