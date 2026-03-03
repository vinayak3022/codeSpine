from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import kuzu

from codespine.config import SETTINGS
from codespine.db.schema import ensure_schema

LOGGER = logging.getLogger(__name__)

_BUFFER_POOL_SIZE = 256 * 1024 * 1024  # 256 MB – small enough for page eviction to work


@dataclass
class GraphStore:
    read_only: bool = False

    def __post_init__(self) -> None:
        db_path = SETTINGS.db_path
        self._tls: threading.local = threading.local()
        try:
            self.db = self._open_db(db_path)
        except Exception as exc:
            fallback = os.path.join("/tmp", ".codespine_db")
            LOGGER.warning("Primary DB path failed (%s). Falling back to %s", exc, fallback)
            self.db = self._open_db(fallback)
        if not self.read_only:
            ensure_schema(self._conn())

    def _open_db(self, path: str) -> kuzu.Database:
        # Newer Kuzu versions accept read_only; fall back for older ones.
        try:
            return kuzu.Database(path, buffer_pool_size=_BUFFER_POOL_SIZE, read_only=self.read_only)
        except TypeError:
            return kuzu.Database(path, buffer_pool_size=_BUFFER_POOL_SIZE)

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
                self.execute("COMMIT")
        except Exception:
            if tx_started:
                self.execute("ROLLBACK")
            raise

    def clear_project(self, project_id: str) -> None:
        # Keep project node and rebuild attached graph artifacts.
        self.execute(
            """
            MATCH (s:Symbol), (f:File)
            WHERE s.file_id = f.id AND f.project_id = $pid
            DETACH DELETE s
            """,
            {"pid": project_id},
        )
        self.execute(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            DETACH DELETE m
            """,
            {"pid": project_id},
        )
        self.execute(
            """
            MATCH (c:Class), (f:File)
            WHERE c.file_id = f.id AND f.project_id = $pid
            DETACH DELETE c
            """,
            {"pid": project_id},
        )
        self.execute(
            """
            MATCH (f:File) WHERE f.project_id = $pid
            DETACH DELETE f
            """,
            {"pid": project_id},
        )

    def upsert_project(self, project_id: str, path: str) -> None:
        self.execute(
            "MERGE (p:Project {id: $id}) SET p.path = $path, p.language = 'java'",
            {"id": project_id, "path": path},
        )

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

    def add_reference(self, rel: str, src_label: str, src_id: str, dst_label: str, dst_id: str, confidence: float) -> None:
        if rel not in {"REFERENCES_TYPE", "IMPLEMENTS", "OVERRIDES"}:
            return
        query = (
            f"MATCH (s:{src_label} {{id: $src_id}}), (d:{dst_label} {{id: $dst_id}}) "
            f"MERGE (s)-[:{rel} {{confidence: $confidence}}]->(d)"
        )
        self.execute(query, {"src_id": src_id, "dst_id": dst_id, "confidence": confidence})

    def set_community(self, community_id: str, label: str, cohesion: float, symbol_ids: list[str]) -> None:
        self.execute(
            "MERGE (c:Community {id: $id}) SET c.label = $label, c.cohesion = $cohesion",
            {"id": community_id, "label": label, "cohesion": cohesion},
        )
        # Batch all symbol→community edges in one transaction to prevent buffer pool exhaustion
        # on large projects (53 K+ symbols would OOM without a single commit boundary).
        with self.transaction():
            for sid in symbol_ids:
                self.execute(
                    "MATCH (s:Symbol {id: $sid}), (c:Community {id: $cid}) MERGE (s)-[:IN_COMMUNITY]->(c)",
                    {"sid": sid, "cid": community_id},
                )

    def set_flow(self, flow_id: str, entry_symbol_id: str, kind: str, symbols_at_depth: list[tuple[str, int]]) -> None:
        self.execute(
            "MERGE (f:Flow {id: $id}) SET f.entry_symbol_id = $entry, f.kind = $kind",
            {"id": flow_id, "entry": entry_symbol_id, "kind": kind},
        )
        for sid, depth in symbols_at_depth:
            self.execute(
                "MATCH (s:Symbol {id: $sid}), (f:Flow {id: $fid}) MERGE (s)-[:IN_FLOW {depth: $depth}]->(f)",
                {"sid": sid, "fid": flow_id, "depth": int(depth)},
            )

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

    def query_records(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        frame = self.execute(query, params or {}).get_as_df()
        if frame.empty:
            return []
        return json.loads(frame.to_json(orient="records"))
