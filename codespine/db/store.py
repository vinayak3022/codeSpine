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
    # Kuzu internal error: abrupt process termination (Ctrl+C) during a write
    # leaves the WAL in an inconsistent state; Kuzu raises this as an
    # IndexError from its internal unordered_map when re-opening the path.
    "unordered_map",
    "key not found",
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
        file_ids = [r["id"] for r in file_recs]
        # Bulk-delete in chunks of 200 to avoid WAL exhaustion on very large projects.
        _CLEAR_CHUNK = 200
        for i in range(0, max(1, len(file_ids)), _CLEAR_CHUNK):
            chunk = file_ids[i: i + _CLEAR_CHUNK]
            if chunk:
                with self.transaction():
                    self.clear_files_batch(chunk)
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
        self.clear_files_batch([file_id])

    def clear_files_batch(self, file_ids: list[str]) -> None:
        """Delete all graph data for a set of files in 4 bulk queries instead of 4×N."""
        if not file_ids:
            return
        fids = list(file_ids)
        self.execute(
            "MATCH (s:Symbol) WHERE s.file_id IN $fids DETACH DELETE s",
            {"fids": fids},
        )
        self.execute(
            """
            MATCH (m:Method), (c:Class)
            WHERE m.class_id = c.id AND c.file_id IN $fids
            DETACH DELETE m
            """,
            {"fids": fids},
        )
        self.execute(
            "MATCH (c:Class) WHERE c.file_id IN $fids DETACH DELETE c",
            {"fids": fids},
        )
        self.execute(
            "MATCH (f:File) WHERE f.id IN $fids DETACH DELETE f",
            {"fids": fids},
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

    def upsert_files_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [{"id": r["id"], "path": r["path"], "project_id": r["project_id"],
                  "is_test": bool(r["is_test"]), "hash": r["hash"]} for r in records]
        if create_mode:
            self.execute(
                """
                UNWIND $rows AS row
                CREATE (f:File {id: row.id, path: row.path, project_id: row.project_id,
                                is_test: row.is_test, hash: row.hash})
                """,
                {"rows": rows},
            )
        else:
            self.execute(
                """
                UNWIND $rows AS row
                MERGE (f:File {id: row.id})
                SET f.path = row.path, f.project_id = row.project_id,
                    f.is_test = row.is_test, f.hash = row.hash
                """,
                {"rows": rows},
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

    def upsert_classes_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [{"id": r["id"], "fqcn": r["fqcn"], "name": r["name"],
                  "package": r["package"], "file_id": r["file_id"]} for r in records]
        if create_mode:
            self.execute(
                """
                UNWIND $rows AS row
                CREATE (c:Class {id: row.id, fqcn: row.fqcn, name: row.name,
                                  package: row.package, file_id: row.file_id})
                """,
                {"rows": rows},
            )
        else:
            self.execute(
                """
                UNWIND $rows AS row
                MERGE (c:Class {id: row.id})
                SET c.fqcn = row.fqcn, c.name = row.name,
                    c.package = row.package, c.file_id = row.file_id
                """,
                {"rows": rows},
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

    def upsert_methods_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [{"id": r["id"], "class_id": r["class_id"], "name": r["name"],
                  "signature": r["signature"], "return_type": r["return_type"],
                  "modifiers": r["modifiers"], "is_constructor": bool(r["is_constructor"]),
                  "is_test": bool(r["is_test"])} for r in records]
        if create_mode:
            # After clear_file, nodes are guaranteed absent — CREATE skips the
            # primary-key existence check that MERGE pays on every row.
            self.execute(
                """
                UNWIND $rows AS row
                MATCH (c:Class {id: row.class_id})
                CREATE (m:Method {id: row.id, class_id: row.class_id, name: row.name,
                                   signature: row.signature, return_type: row.return_type,
                                   modifiers: row.modifiers, is_constructor: row.is_constructor,
                                   is_test: row.is_test})
                CREATE (c)-[:HAS_METHOD]->(m)
                """,
                {"rows": rows},
            )
        else:
            self.execute(
                """
                UNWIND $rows AS row
                MATCH (c:Class {id: row.class_id})
                MERGE (m:Method {id: row.id})
                SET m.class_id = row.class_id, m.name = row.name,
                    m.signature = row.signature, m.return_type = row.return_type,
                    m.modifiers = row.modifiers, m.is_constructor = row.is_constructor,
                    m.is_test = row.is_test
                MERGE (c)-[:HAS_METHOD]->(m)
                """,
                {"rows": rows},
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

    def upsert_symbols_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [{"id": r["id"], "kind": r["kind"], "name": r["name"],
                  "fqname": r["fqname"], "file_id": r["file_id"],
                  "line": int(r["line"]), "col": int(r["col"]),
                  "embedding": r.get("embedding")} for r in records]
        if create_mode:
            self.execute(
                """
                UNWIND $rows AS row
                MATCH (f:File {id: row.file_id})
                CREATE (s:Symbol {id: row.id, kind: row.kind, name: row.name,
                                   fqname: row.fqname, file_id: row.file_id,
                                   line: row.line, col: row.col, embedding: row.embedding})
                CREATE (f)-[:DECLARES]->(s)
                """,
                {"rows": rows},
            )
        else:
            self.execute(
                """
                UNWIND $rows AS row
                MATCH (f:File {id: row.file_id})
                MERGE (s:Symbol {id: row.id})
                SET s.kind = row.kind, s.name = row.name, s.fqname = row.fqname,
                    s.file_id = row.file_id, s.line = row.line, s.col = row.col,
                    s.embedding = row.embedding
                MERGE (f)-[:DECLARES]->(s)
                """,
                {"rows": rows},
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

    def add_calls_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [{"source_id": r["source_id"], "target_id": r["target_id"],
                  "confidence": float(r["confidence"]), "reason": r["reason"]}
                 for r in records]
        op = "CREATE" if create_mode else "MERGE"
        self.execute(
            f"""
            UNWIND $rows AS row
            MATCH (src:Method {{id: row.source_id}}), (dst:Method {{id: row.target_id}})
            {op} (src)-[:CALLS {{confidence: row.confidence, reason: row.reason}}]->(dst)
            """,
            {"rows": rows},
        )

    def add_reference(self, rel: str, src_label: str, src_id: str, dst_label: str, dst_id: str, confidence: float) -> None:
        if rel not in {"REFERENCES_TYPE", "IMPLEMENTS", "OVERRIDES"}:
            return
        query = (
            f"MATCH (s:{src_label} {{id: $src_id}}), (d:{dst_label} {{id: $dst_id}}) "
            f"MERGE (s)-[:{rel} {{confidence: $confidence}}]->(d)"
        )
        self.execute(query, {"src_id": src_id, "dst_id": dst_id, "confidence": confidence})

    def add_references_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        # Group by (rel, src_label, dst_label) so each group can use a single UNWIND.
        from collections import defaultdict
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for rec in records:
            rel = rec.get("rel")
            if rel not in {"REFERENCES_TYPE", "IMPLEMENTS", "OVERRIDES"}:
                continue
            groups[(rel, rec["src_label"], rec["dst_label"])].append(
                {"src_id": rec["src_id"], "dst_id": rec["dst_id"],
                 "confidence": float(rec["confidence"])}
            )
        op = "CREATE" if create_mode else "MERGE"
        for (rel, src_label, dst_label), batch in groups.items():
            self.execute(
                f"UNWIND $rows AS row "
                f"MATCH (s:{src_label} {{id: row.src_id}}), (d:{dst_label} {{id: row.dst_id}}) "
                f"{op} (s)-[:{rel} {{confidence: row.confidence}}]->(d)",
                {"rows": batch},
            )

    def add_injection(
        self,
        src_class_id: str,
        dst_class_id: str,
        framework: str,
        binding_type: str,
        confidence: float,
    ) -> None:
        """Write an INJECTS edge between two Class nodes."""
        try:
            self.execute(
                """
                MATCH (a:Class {id: $src}), (b:Class {id: $dst})
                MERGE (a)-[:INJECTS {framework: $fw, binding_type: $bt, confidence: $conf}]->(b)
                """,
                {
                    "src": src_class_id,
                    "dst": dst_class_id,
                    "fw": framework,
                    "bt": binding_type,
                    "conf": float(confidence),
                },
            )
        except Exception as exc:
            LOGGER.debug("add_injection: skipping edge %s→%s: %s", src_class_id, dst_class_id, exc)

    def add_injections_batch(self, records: list[dict[str, Any]]) -> None:
        for rec in records:
            self.add_injection(
                src_class_id=rec["src"],
                dst_class_id=rec["dst"],
                framework=rec.get("framework", "unknown"),
                binding_type=rec.get("binding_type", "unknown"),
                confidence=float(rec.get("confidence", 0.8)),
            )

    def add_interface_binding(
        self,
        src_class_id: str,
        dst_class_id: str,
        confidence: float,
        reason: str,
    ) -> None:
        """Write a BINDS_INTERFACE edge between two Class nodes."""
        try:
            self.execute(
                """
                MATCH (a:Class {id: $src}), (b:Class {id: $dst})
                MERGE (a)-[:BINDS_INTERFACE {confidence: $conf, reason: $reason}]->(b)
                """,
                {
                    "src": src_class_id,
                    "dst": dst_class_id,
                    "conf": float(confidence),
                    "reason": reason,
                },
            )
        except Exception as exc:
            LOGGER.debug("add_interface_binding: skipping edge %s→%s: %s", src_class_id, dst_class_id, exc)

    def add_interface_bindings_batch(self, records: list[dict[str, Any]]) -> None:
        for rec in records:
            self.add_interface_binding(
                src_class_id=rec["src"],
                dst_class_id=rec["dst"],
                confidence=float(rec.get("confidence", 0.9)),
                reason=rec.get("reason", "implements"),
            )

    # Sub-batch sizes for direct-to-graph file writes (same policy as engine.py)
    _FILE_METHOD_SUB_BATCH = 200
    _FILE_SYMBOL_SUB_BATCH = 200
    _FILE_CALL_SUB_BATCH = 500
    _FILE_REL_SUB_BATCH = 500

    def upsert_file_from_entry(self, entry: dict, project_path: str) -> None:
        """Atomically replace one file's graph data from a build_overlay_file_entry() dict.

        Clears all existing nodes/edges for the file first, then writes the
        full parsed content (file, classes, methods, symbols, calls, type rels)
        in sub-batched transactions to prevent Kuzu buffer pool OOM.

        This is the primary path for watch-mode incremental writes — it
        bypasses the overlay JSON store and writes directly to the write DB
        so changes are immediately visible after snapshot_to_read_replica().
        """
        f_id = entry["file_id"]
        path = entry["file_path"]
        project_id = entry["project_id"]
        is_test = bool(entry.get("is_test", False))
        digest = entry.get("file_hash", "")
        classes = entry.get("classes") or []
        methods = entry.get("methods") or []
        symbols = entry.get("symbols") or []
        calls = entry.get("calls") or []
        type_rels = entry.get("types") or []

        # 1. Clear stale data for this file
        with self.transaction():
            self.clear_file(f_id)
        self._recycle_conn()

        # 2. Upsert file record
        with self.transaction():
            self.upsert_files_batch(
                [{"id": f_id, "path": path, "project_id": project_id,
                  "is_test": is_test, "hash": digest}],
            )
        self._recycle_conn()

        # 3. Upsert classes (typically very few per file)
        if classes:
            with self.transaction():
                self.upsert_classes_batch(classes)
            self._recycle_conn()

        # 4. Upsert methods in sub-batches of 200
        for i in range(0, len(methods), self._FILE_METHOD_SUB_BATCH):
            with self.transaction():
                self.upsert_methods_batch(methods[i: i + self._FILE_METHOD_SUB_BATCH])
            self._recycle_conn()

        # 5. Upsert symbols in sub-batches of 200
        for i in range(0, len(symbols), self._FILE_SYMBOL_SUB_BATCH):
            with self.transaction():
                self.upsert_symbols_batch(symbols[i: i + self._FILE_SYMBOL_SUB_BATCH])
            self._recycle_conn()

        # 6. Write call edges in sub-batches of 500 (normalise key names to match add_calls_batch)
        for i in range(0, len(calls), self._FILE_CALL_SUB_BATCH):
            batch = calls[i: i + self._FILE_CALL_SUB_BATCH]
            normalised = [
                {"source_id": rec["src"], "target_id": rec["dst"],
                 "confidence": float(rec.get("confidence", 0.5)),
                 "reason": rec.get("reason", "unknown")}
                for rec in batch
            ]
            with self.transaction():
                self.add_calls_batch(normalised)
            self._recycle_conn()

        # 7. Write type relations (IMPLEMENTS, OVERRIDES, REFERENCES_TYPE)
        for i in range(0, len(type_rels), self._FILE_REL_SUB_BATCH):
            batch = type_rels[i: i + self._FILE_REL_SUB_BATCH]
            with self.transaction():
                self.add_references_batch(batch)
            self._recycle_conn()

    def clear_file_by_path(self, project_id: str, project_path: str, file_path: str) -> None:
        """Delete all graph data for a file identified by its filesystem path."""
        from codespine.indexer.symbol_builder import file_id as _fid
        import os as _os
        rel_path = _os.path.relpath(os.path.abspath(file_path), os.path.abspath(project_path))
        f_id = _fid(project_id, rel_path)
        with self.transaction():
            self.clear_file(f_id)
        self._recycle_conn()

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

    @staticmethod
    def force_delete_all_data() -> list[str]:
        """Delete all CodeSpine data files without touching the Kuzu engine.

        This is the nuclear option for OOM recovery: when the buffer pool is
        exhausted, normal DB writes (including reset_project / clear_project)
        also fail.  This bypasses Kuzu entirely by removing the data files
        from disk, allowing a fresh start.

        Returns the list of paths that were removed.
        """
        removed: list[str] = []
        for path in [
            SETTINGS.db_path,
            SETTINGS.db_snapshot_path,
            SETTINGS.db_snapshot_path + ".updated",
            SETTINGS.db_snapshot_path + ".tmp",
            SETTINGS.embedding_cache_path,
            SETTINGS.overlay_dir,
            SETTINGS.index_meta_dir,
        ]:
            if not os.path.exists(path):
                continue
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
                removed.append(path)
            except OSError:
                pass
        # Also remove any stale WAL files next to the DB
        for suffix in (".wal", ".lock"):
            wal_path = SETTINGS.db_path + suffix
            if os.path.exists(wal_path):
                try:
                    os.remove(wal_path)
                    removed.append(wal_path)
                except OSError:
                    pass
        return removed

    def rebuild_empty_db(self) -> None:
        self._recycle_conn()
        path = SETTINGS.db_path
        # Remove the DB directory AND any stale WAL / lock files
        self._remove_db_path(path)
        for suffix in (".wal", ".lock"):
            sidecar = path + suffix
            if os.path.exists(sidecar):
                try:
                    os.remove(sidecar)
                except OSError:
                    pass

        # Also remove the read replica so that read-only callers (stats, MCP)
        # don't continue to see stale data from before the wipe.
        for stale in [
            SETTINGS.db_snapshot_path,
            SETTINGS.db_snapshot_path + ".tmp",
            SETTINGS.db_snapshot_path + ".updated",
        ]:
            self._remove_db_path(stale)

        # Kuzu may retain stale internal state from a previous failed open of
        # this path (e.g. after Ctrl+C mid-write).  The in-process C++ state
        # is poisoned and will raise "unordered_map::at: key not found" on any
        # new kuzu.Database() call — even for a freshly deleted path.
        #
        # Strategy: try primary → try /tmp fallback → force-delete everything
        # and re-import kuzu to get a clean C++ state.
        try:
            self.db = self._open_db(path)
        except Exception as exc1:
            LOGGER.warning("rebuild_empty_db: primary path failed (%s)", exc1)
            fallback = os.path.join("/tmp", ".codespine_db")
            self._remove_db_path(fallback)
            for suffix in (".wal", ".lock"):
                sidecar = fallback + suffix
                if os.path.exists(sidecar):
                    try:
                        os.remove(sidecar)
                    except OSError:
                        pass
            try:
                self.db = self._open_db(fallback)
            except Exception as exc2:
                # Nuclear option: force-delete all files and reimport kuzu
                # so the C++ runtime starts from a completely clean state.
                LOGGER.warning("rebuild_empty_db: fallback also failed (%s); force-resetting", exc2)
                self.force_delete_all_data()
                import importlib
                importlib.reload(kuzu)
                try:
                    self.db = kuzu.Database(path, buffer_pool_size=_WRITE_BUFFER_POOL_SIZE)
                except TypeError:
                    self.db = kuzu.Database(path)
        self._tls = threading.local()
        ensure_schema(self._conn())
        # Force Kuzu to flush the WAL to the main data files so that a
        # subsequent read-only open (stats, MCP snapshot) can see the schema
        # without needing WAL replay (which read-only mode cannot do).
        try:
            self._conn().execute("CHECKPOINT")
        except Exception:
            pass

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

    def upsert_coupling(self, file_a: str, file_b: str, strength: float, cochanges: int, days: int) -> None:
        self.execute(
            """
            MATCH (a:File {id: $a}), (b:File {id: $b})
            MERGE (a)-[:CO_CHANGED_WITH {strength: $strength, cochanges: $cochanges, days: $days}]->(b)
            """,
            {
                "a": file_a,
                "b": file_b,
                "strength": strength,
                "cochanges": int(cochanges),
                "days": int(days),
            },
        )

    # Lock and flag for background snapshot coalescing.
    # Only one snapshot runs at a time; a pending request supersedes queued ones.
    _snapshot_lock: threading.Lock = threading.Lock()
    _snapshot_pending: threading.Event = threading.Event()

    @staticmethod
    def snapshot_to_read_replica(background: bool = False) -> bool:
        """Atomically copy the write DB to the read-replica path.

        The read replica is used by the MCP daemon and all read-only CLI
        commands so they never contend with the write process's buffer pool.

        Parameters
        ----------
        background:
            When True the copy runs in a daemon thread and this call returns
            immediately (always returns True). Only one copy runs at a time;
            rapid successive background calls are coalesced — the next copy
            starts only after the current one finishes, so the sentinel is
            always written with the *latest* data.

        Returns True on success (or when dispatched to background), False if
        the source DB does not exist.
        """
        src = SETTINGS.db_path
        if not os.path.exists(src):
            return False

        if background:
            # Signal that a snapshot is wanted, then ensure a worker is running.
            GraphStore._snapshot_pending.set()

            def _worker() -> None:
                while GraphStore._snapshot_pending.is_set():
                    GraphStore._snapshot_pending.clear()
                    with GraphStore._snapshot_lock:
                        GraphStore._do_snapshot()

            if not GraphStore._snapshot_lock.locked():
                t = threading.Thread(target=_worker, daemon=True, name="codespine-snapshot")
                t.start()
            return True

        # Foreground (blocking) path — used by CLI analyse and tests.
        with GraphStore._snapshot_lock:
            return GraphStore._do_snapshot()

    @staticmethod
    def _do_snapshot() -> bool:
        """Perform the actual copy.  Must be called with _snapshot_lock held."""
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
        try:
            frame = self.execute(query, params or {}).get_as_df()
        except RuntimeError as exc:
            # In read-only mode the DB may have been cleared but the schema
            # hasn't been flushed from WAL, or the DB is brand-new.  Return
            # an empty result instead of crashing callers (stats, MCP, etc.).
            if self.read_only and "does not exist" in str(exc).lower():
                return []
            raise
        if frame.empty:
            return []
        return json.loads(frame.to_json(orient="records"))
