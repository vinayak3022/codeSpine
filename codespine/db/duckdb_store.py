"""DuckDB-backed graph store for CodeSpine.

Drop-in replacement for ``GraphStore`` (Kuzu) with the same write API and
SQL-based reads.  DuckDB gives 10-50× faster batch writes vs Kuzu's
property-graph MERGE/UNWIND approach — the primary bottleneck for large repos.

Schema
------
Flat relational tables (no property-graph tables).  Relationships are stored
as edge tables (calls, references_type, injects, binds_interface, …).
Each table has a ``VARCHAR`` primary key that matches the stable SHA1 IDs
produced by ``codespine.indexer.symbol_builder``.

Usage
-----
Select the backend via the ``CODESPINE_BACKEND`` environment variable::

    CODESPINE_BACKEND=duckdb codespine analyse /path/to/project

Or via ``ShardedGraphStore(backend="duckdb")``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from typing import Any

import duckdb

from codespine.config import SETTINGS

LOGGER = logging.getLogger(__name__)

_VECTOR_DIM = SETTINGS.vector_dim  # 384


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key     VARCHAR PRIMARY KEY,
    value   VARCHAR
);

CREATE TABLE IF NOT EXISTS projects (
    id              VARCHAR PRIMARY KEY,
    path            VARCHAR,
    language        VARCHAR,
    indexed_at      VARCHAR,
    indexed_commit  VARCHAR,
    overlay_dirty   BOOLEAN
);

CREATE TABLE IF NOT EXISTS files (
    id          VARCHAR PRIMARY KEY,
    path        VARCHAR,
    project_id  VARCHAR,
    is_test     BOOLEAN,
    hash        VARCHAR
);

CREATE TABLE IF NOT EXISTS classes (
    id          VARCHAR PRIMARY KEY,
    fqcn        VARCHAR,
    name        VARCHAR,
    package     VARCHAR,
    file_id     VARCHAR
);

CREATE TABLE IF NOT EXISTS methods (
    id              VARCHAR PRIMARY KEY,
    class_id        VARCHAR,
    name            VARCHAR,
    signature       VARCHAR,
    return_type     VARCHAR,
    modifiers       VARCHAR[],
    is_constructor  BOOLEAN,
    is_test         BOOLEAN
);

CREATE TABLE IF NOT EXISTS symbols (
    id          VARCHAR PRIMARY KEY,
    kind        VARCHAR,
    name        VARCHAR,
    fqname      VARCHAR,
    file_id     VARCHAR,
    line        INTEGER,
    col         INTEGER,
    embedding   FLOAT[{_VECTOR_DIM}]
);

-- Edge tables
CREATE TABLE IF NOT EXISTS calls (
    source_id   VARCHAR,
    target_id   VARCHAR,
    confidence  DOUBLE,
    reason      VARCHAR,
    PRIMARY KEY (source_id, target_id)
);

CREATE TABLE IF NOT EXISTS references_type (
    src_id      VARCHAR,
    dst_id      VARCHAR,
    rel         VARCHAR,
    confidence  DOUBLE,
    PRIMARY KEY (src_id, dst_id, rel)
);

CREATE TABLE IF NOT EXISTS injects (
    src_class_id    VARCHAR,
    dst_class_id    VARCHAR,
    framework       VARCHAR,
    binding_type    VARCHAR,
    confidence      DOUBLE,
    PRIMARY KEY (src_class_id, dst_class_id)
);

CREATE TABLE IF NOT EXISTS binds_interface (
    src_class_id    VARCHAR,
    dst_class_id    VARCHAR,
    confidence      DOUBLE,
    reason          VARCHAR,
    PRIMARY KEY (src_class_id, dst_class_id)
);

CREATE TABLE IF NOT EXISTS communities (
    id          VARCHAR PRIMARY KEY,
    label       VARCHAR,
    cohesion    DOUBLE
);

CREATE TABLE IF NOT EXISTS community_members (
    symbol_id       VARCHAR,
    community_id    VARCHAR,
    PRIMARY KEY (symbol_id, community_id)
);

CREATE TABLE IF NOT EXISTS flows (
    id                  VARCHAR PRIMARY KEY,
    entry_symbol_id     VARCHAR,
    kind                VARCHAR
);

CREATE TABLE IF NOT EXISTS flow_members (
    symbol_id   VARCHAR,
    flow_id     VARCHAR,
    depth       INTEGER,
    PRIMARY KEY (symbol_id, flow_id)
);

CREATE TABLE IF NOT EXISTS co_changed_with (
    file_a      VARCHAR,
    file_b      VARCHAR,
    strength    DOUBLE,
    cochanges   INTEGER,
    days        INTEGER,
    PRIMARY KEY (file_a, file_b)
);
"""


class DuckDBStore:
    """DuckDB-backed store implementing the same write/read API as GraphStore.

    Parameters
    ----------
    read_only:
        Open the DB in read-only mode (uses the snapshot path if available).
    db_path_override:
        Override the default ``SETTINGS.db_path``.
    snapshot_path_override:
        Override the default ``SETTINGS.db_snapshot_path``.
    """

    backend: str = "duckdb"

    def __init__(
        self,
        read_only: bool = False,
        db_path_override: str | None = None,
        snapshot_path_override: str | None = None,
    ) -> None:
        self.read_only = read_only
        self._db_path: str = db_path_override or SETTINGS.db_path
        self._snapshot_path: str = snapshot_path_override or SETTINGS.db_snapshot_path

        self._lock = threading.Lock()
        self._inst_snapshot_lock = threading.Lock()
        self._inst_snapshot_pending = threading.Event()

        # Fake overlay_store so analysis code that checks for it works.
        from codespine.overlay.store import OverlayStore
        self.overlay_store = OverlayStore()

        # Prefer snapshot for read-only access; fall back to write path.
        snap_exists = os.path.exists(self._snapshot_path)
        db_file = self._snapshot_path if read_only and snap_exists else self._db_path

        # ----------------------------------------------------------------
        # Legacy KùzuDB migration guard
        # KùzuDB stores its database as a *directory*; DuckDB uses a single
        # file.  When CODESPINE_BACKEND is changed to "duckdb" an existing
        # KùzuDB shard directory sits at the same path and DuckDB refuses to
        # open it.  Detect this case, wipe the directory, and start fresh.
        # ----------------------------------------------------------------
        for legacy_path in (self._db_path, self._snapshot_path):
            if os.path.isdir(legacy_path):
                LOGGER.info(
                    "Removing legacy KùzuDB directory at %s — "
                    "re-index with 'codespine analyse' to rebuild.",
                    legacy_path,
                )
                shutil.rmtree(legacy_path)

        # Re-evaluate db_file after possible cleanup.
        snap_exists = os.path.exists(self._snapshot_path)
        db_file = self._snapshot_path if read_only and snap_exists else self._db_path

        # When opening read-only and no snapshot exists yet, open the write
        # DB in read-only mode so callers get an empty-but-valid store rather
        # than an error.
        if read_only and not snap_exists and not os.path.exists(self._db_path):
            # No data at all — open an in-memory DB so queries return [] cleanly.
            self._conn = duckdb.connect(":memory:")
            self._ensure_schema()
            return

        os.makedirs(os.path.dirname(db_file) or ".", exist_ok=True)
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(db_file, read_only=read_only)
        if not read_only:
            self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        # DuckDB's execute() handles one statement at a time.
        for stmt in _SCHEMA_SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                try:
                    self._conn.execute(stmt)
                except Exception as exc:
                    LOGGER.debug("Schema DDL skipped (%s): %.80s", exc, stmt)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta VALUES ('version', '1')"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Low-level execute helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: dict[str, Any] | None = None):
        """Execute a SQL statement.  Params are passed as a list in positional order."""
        if params:
            # Convert dict params to list (DuckDB uses $1, $2 or positional ?)
            self._conn.execute(sql, list(params.values()) if isinstance(params, dict) else params)
        else:
            self._conn.execute(sql)

    def query_records(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a SQL (or Cypher) query and return rows as a list of dicts.

        When *query* looks like Cypher (starts with MATCH), it is transparently
        translated to DuckDB SQL via ``_cypher_compat.translate`` before execution.
        After translation the SQL uses ``$name`` named placeholders which DuckDB
        accepts when the params dict is passed directly.

        For callers that pass raw SQL with positional ``?`` placeholders (e.g.,
        tests and internal helpers), a dict is safely converted to a value list
        so the existing call-sites never need to change.
        """
        from codespine.db._cypher_compat import is_cypher, translate  # lazy import

        use_named_params = False
        if is_cypher(query):
            query, params = translate(query, params)
            # translate() preserves/returns a dict; SQL uses $name placeholders.
            use_named_params = True

        if params:
            if use_named_params and isinstance(params, dict):
                # Named $param style — DuckDB accepts dict directly
                rel = self._conn.execute(query, params)
            else:
                # Positional ? style — DuckDB requires a list
                param_vals = list(params.values()) if isinstance(params, dict) else list(params)
                rel = self._conn.execute(query, param_vals)
        else:
            rel = self._conn.execute(query)
        cols = [d[0] for d in rel.description or []]
        return [dict(zip(cols, row)) for row in rel.fetchall()]

    def _executemany(self, sql: str, rows: list[list]) -> None:
        """Bulk insert using DuckDB's executemany (fast batch path)."""
        if not rows:
            return
        self._conn.executemany(sql, rows)

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN TRANSACTION")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def _recycle_conn(self) -> None:
        """No-op for DuckDB — connections are cheap and don't accumulate pages."""

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def stable_id(*parts: str) -> str:
        raw = "::".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Project operations
    # ------------------------------------------------------------------

    def upsert_project(self, project_id: str, path: str) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO projects (id, path, language, indexed_at, indexed_commit, overlay_dirty)
            VALUES (?, ?, 'java', ?, '', false)
            """,
            [project_id, path, str(int(time.time()))],
        )

    def set_project_overlay_dirty(self, project_id: str, dirty: bool) -> None:
        self._conn.execute(
            "UPDATE projects SET overlay_dirty=? WHERE id=?", [bool(dirty), project_id]
        )

    def set_project_indexed_commit(self, project_id: str, commit: str) -> None:
        self._conn.execute(
            "UPDATE projects SET indexed_commit=?, indexed_at=? WHERE id=?",
            [commit, str(int(time.time())), project_id],
        )

    def get_project_metadata(self, project_id: str) -> dict[str, Any] | None:
        rows = self.query_records(
            "SELECT id, path, language, indexed_at, indexed_commit, overlay_dirty FROM projects WHERE id=?",
            {"id": project_id},
        )
        return rows[0] if rows else None

    def list_project_metadata(self) -> list[dict[str, Any]]:
        return self.query_records(
            "SELECT id, path, language, indexed_at, indexed_commit, overlay_dirty FROM projects ORDER BY id"
        )

    def project_file_hashes(self, project_id: str) -> dict[str, dict[str, str]]:
        rows = self.query_records(
            "SELECT id, path, hash FROM files WHERE project_id=?",
            {"pid": project_id},
        )
        return {r["id"]: {"path": r.get("path", ""), "hash": r.get("hash", "")} for r in rows}

    def project_has_embeddings(self, project_id: str) -> bool:
        rows = self.query_records(
            """
            SELECT count(s.id) AS cnt FROM symbols s
            JOIN files f ON s.file_id = f.id
            WHERE f.project_id = ? AND s.embedding IS NOT NULL
            """,
            {"pid": project_id},
        )
        return bool(rows and int(rows[0].get("cnt") or 0) > 0)

    def clear_project(self, project_id: str) -> None:
        with self.transaction():
            self._conn.execute(
                """
                DELETE FROM symbols WHERE file_id IN (SELECT id FROM files WHERE project_id=?)
                """,
                [project_id],
            )
            self._conn.execute(
                """
                DELETE FROM calls WHERE source_id IN (
                    SELECT m.id FROM methods m
                    JOIN classes c ON m.class_id = c.id
                    JOIN files f ON c.file_id = f.id
                    WHERE f.project_id = ?
                )
                """,
                [project_id],
            )
            self._conn.execute(
                """
                DELETE FROM methods WHERE class_id IN (
                    SELECT c.id FROM classes c
                    JOIN files f ON c.file_id = f.id WHERE f.project_id = ?
                )
                """,
                [project_id],
            )
            self._conn.execute(
                "DELETE FROM classes WHERE file_id IN (SELECT id FROM files WHERE project_id=?)",
                [project_id],
            )
            self._conn.execute("DELETE FROM files WHERE project_id=?", [project_id])
            self._conn.execute("DELETE FROM projects WHERE id=?", [project_id])

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def clear_file(self, file_id: str) -> None:
        self.clear_files_batch([file_id])

    def clear_files_batch(self, file_ids: list[str]) -> None:
        if not file_ids:
            return
        # Use IN with unnest for bulk delete.
        ph = ", ".join("?" * len(file_ids))
        self._conn.execute(f"DELETE FROM symbols WHERE file_id IN ({ph})", file_ids)
        # Methods: go via classes
        self._conn.execute(
            f"DELETE FROM methods WHERE class_id IN "
            f"(SELECT id FROM classes WHERE file_id IN ({ph}))",
            file_ids,
        )
        self._conn.execute(f"DELETE FROM classes WHERE file_id IN ({ph})", file_ids)
        self._conn.execute(f"DELETE FROM files WHERE id IN ({ph})", file_ids)

    def upsert_files_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [
            [r["id"], r["path"], r["project_id"], bool(r["is_test"]), r["hash"]]
            for r in records
        ]
        self._executemany(
            "INSERT OR REPLACE INTO files (id, path, project_id, is_test, hash) VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    def upsert_file(self, file_id: str, path: str, project_id: str, is_test: bool, digest: str) -> None:
        self.upsert_files_batch(
            [{"id": file_id, "path": path, "project_id": project_id, "is_test": is_test, "hash": digest}]
        )

    # ------------------------------------------------------------------
    # Class operations
    # ------------------------------------------------------------------

    def upsert_classes_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [[r["id"], r["fqcn"], r["name"], r["package"], r["file_id"]] for r in records]
        self._executemany(
            "INSERT OR REPLACE INTO classes (id, fqcn, name, package, file_id) VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    def upsert_class(self, class_id: str, fqcn: str, name: str, package: str, file_id: str) -> None:
        self.upsert_classes_batch([{"id": class_id, "fqcn": fqcn, "name": name, "package": package, "file_id": file_id}])

    # ------------------------------------------------------------------
    # Method operations
    # ------------------------------------------------------------------

    def upsert_methods_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [
            [
                r["id"], r["class_id"], r["name"], r["signature"],
                r["return_type"], r.get("modifiers") or [],
                bool(r["is_constructor"]), bool(r["is_test"]),
            ]
            for r in records
        ]
        self._executemany(
            """
            INSERT OR REPLACE INTO methods
                (id, class_id, name, signature, return_type, modifiers, is_constructor, is_test)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def list_methods(self) -> list[dict[str, Any]]:
        return self.query_records(
            "SELECT m.id AS method_id, m.name, m.signature, c.fqcn AS class_fqcn "
            "FROM methods m JOIN classes c ON m.class_id = c.id"
        )

    # ------------------------------------------------------------------
    # Symbol operations
    # ------------------------------------------------------------------

    def upsert_symbols_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows_emb = []
        rows_no_emb = []
        for r in records:
            emb = r.get("embedding")
            row_base = [r["id"], r["kind"], r["name"], r["fqname"], r["file_id"], int(r["line"]), int(r["col"])]
            if emb is not None:
                rows_emb.append(row_base + [emb])
            else:
                rows_no_emb.append(row_base)

        if rows_no_emb:
            self._executemany(
                "INSERT OR REPLACE INTO symbols (id, kind, name, fqname, file_id, line, col) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows_no_emb,
            )
        if rows_emb:
            self._executemany(
                "INSERT OR REPLACE INTO symbols (id, kind, name, fqname, file_id, line, col, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows_emb,
            )

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    def add_calls_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [
            [r["source_id"], r["target_id"], float(r["confidence"]), r["reason"]]
            for r in records
        ]
        self._executemany(
            "INSERT OR REPLACE INTO calls (source_id, target_id, confidence, reason) VALUES (?, ?, ?, ?)",
            rows,
        )

    def add_call(self, source_id: str, target_id: str, confidence: float, reason: str) -> None:
        self.add_calls_batch([{"source_id": source_id, "target_id": target_id, "confidence": confidence, "reason": reason}])

    def add_references_batch(self, records: list[dict[str, Any]], create_mode: bool = False) -> None:
        if not records:
            return
        rows = [
            [r["src_id"], r["dst_id"], r.get("rel", "REFERENCES_TYPE"), float(r.get("confidence", 0.9))]
            for r in records
            if r.get("rel") in {"REFERENCES_TYPE", "IMPLEMENTS", "OVERRIDES"}
        ]
        if rows:
            self._executemany(
                "INSERT OR REPLACE INTO references_type (src_id, dst_id, rel, confidence) VALUES (?, ?, ?, ?)",
                rows,
            )

    def add_reference(self, rel: str, src_label: str, src_id: str, dst_label: str, dst_id: str, confidence: float) -> None:
        if rel in {"REFERENCES_TYPE", "IMPLEMENTS", "OVERRIDES"}:
            self.add_references_batch([{"src_id": src_id, "dst_id": dst_id, "rel": rel, "confidence": confidence}])

    def add_injections_batch(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        rows = [[r["src"], r["dst"], r.get("framework", ""), r.get("binding_type", ""), float(r.get("confidence", 0.8))] for r in records]
        self._executemany(
            "INSERT OR REPLACE INTO injects (src_class_id, dst_class_id, framework, binding_type, confidence) VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    def add_injection(self, src_class_id, dst_class_id, framework, binding_type, confidence):
        self.add_injections_batch([{"src": src_class_id, "dst": dst_class_id, "framework": framework, "binding_type": binding_type, "confidence": confidence}])

    def add_interface_bindings_batch(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        rows = [[r["src"], r["dst"], float(r.get("confidence", 0.9)), r.get("reason", "")] for r in records]
        self._executemany(
            "INSERT OR REPLACE INTO binds_interface (src_class_id, dst_class_id, confidence, reason) VALUES (?, ?, ?, ?)",
            rows,
        )

    def add_interface_binding(self, src_class_id, dst_class_id, confidence, reason):
        self.add_interface_bindings_batch([{"src": src_class_id, "dst": dst_class_id, "confidence": confidence, "reason": reason}])

    # ------------------------------------------------------------------
    # Community / Flow / Coupling
    # ------------------------------------------------------------------

    def clear_communities(self) -> None:
        self._conn.execute("DELETE FROM community_members")
        self._conn.execute("DELETE FROM communities")

    def clear_flows(self) -> None:
        self._conn.execute("DELETE FROM flow_members")
        self._conn.execute("DELETE FROM flows")

    def clear_coupling(self) -> None:
        self._conn.execute("DELETE FROM co_changed_with")

    def clear_analysis_artifacts(self) -> None:
        self.clear_communities()
        self.clear_flows()
        self.clear_coupling()

    def set_community(self, community_id: str, label: str, cohesion: float, symbol_ids: list[str]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO communities (id, label, cohesion) VALUES (?, ?, ?)",
            [community_id, label, float(cohesion)],
        )
        if symbol_ids:
            rows = [[sid, community_id] for sid in symbol_ids]
            self._executemany(
                "INSERT OR REPLACE INTO community_members (symbol_id, community_id) VALUES (?, ?)",
                rows,
            )

    def set_flow(self, flow_id: str, entry_symbol_id: str, kind: str, symbols_at_depth: list[tuple[str, int]]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO flows (id, entry_symbol_id, kind) VALUES (?, ?, ?)",
            [flow_id, entry_symbol_id, kind],
        )
        if symbols_at_depth:
            rows = [[sid, flow_id, int(depth)] for sid, depth in symbols_at_depth]
            self._executemany(
                "INSERT OR REPLACE INTO flow_members (symbol_id, flow_id, depth) VALUES (?, ?, ?)",
                rows,
            )

    def upsert_coupling(self, file_a: str, file_b: str, strength: float, cochanges: int, days: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO co_changed_with (file_a, file_b, strength, cochanges, days) VALUES (?, ?, ?, ?, ?)",
            [file_a, file_b, float(strength), int(cochanges), int(days)],
        )

    # ------------------------------------------------------------------
    # Composite write
    # ------------------------------------------------------------------

    _FILE_METHOD_SUB_BATCH = 200
    _FILE_SYMBOL_SUB_BATCH = 200
    _FILE_CALL_SUB_BATCH = 500
    _FILE_REL_SUB_BATCH = 500

    def upsert_file_from_entry(self, entry: dict, project_path: str) -> None:
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

        self.clear_file(f_id)
        self.upsert_files_batch([{"id": f_id, "path": path, "project_id": project_id, "is_test": is_test, "hash": digest}])
        if classes:
            self.upsert_classes_batch(classes)
        for i in range(0, len(methods), self._FILE_METHOD_SUB_BATCH):
            self.upsert_methods_batch(methods[i: i + self._FILE_METHOD_SUB_BATCH])
        for i in range(0, len(symbols), self._FILE_SYMBOL_SUB_BATCH):
            self.upsert_symbols_batch(symbols[i: i + self._FILE_SYMBOL_SUB_BATCH])
        for i in range(0, len(calls), self._FILE_CALL_SUB_BATCH):
            batch = calls[i: i + self._FILE_CALL_SUB_BATCH]
            self.add_calls_batch([
                {"source_id": r["src"], "target_id": r["dst"],
                 "confidence": float(r.get("confidence", 0.5)), "reason": r.get("reason", "")}
                for r in batch
            ])
        for i in range(0, len(type_rels), self._FILE_REL_SUB_BATCH):
            self.add_references_batch(type_rels[i: i + self._FILE_REL_SUB_BATCH])

    def clear_file_by_path(self, project_id: str, project_path: str, file_path: str) -> None:
        from codespine.indexer.symbol_builder import file_id as _fid
        import os as _os
        rel_path = _os.path.relpath(_os.path.abspath(file_path), _os.path.abspath(project_path))
        f_id = _fid(project_id, rel_path)
        self.clear_file(f_id)

    # ------------------------------------------------------------------
    # Analysis read helpers (SQL)
    # The analysis modules call these typed methods rather than raw query_records.
    # ------------------------------------------------------------------

    def get_symbols_for_search(self, project_id: str | None = None) -> list[dict]:
        """Return all symbols with file metadata — used by hybrid_search."""
        if project_id:
            return self.query_records(
                """
                SELECT s.id, s.kind, s.name, s.fqname, s.embedding,
                       s.line, s.file_id,
                       f.path AS file_path, f.project_id, f.is_test
                FROM symbols s JOIN files f ON s.file_id = f.id
                WHERE f.project_id = ?
                """,
                {"pid": project_id},
            )
        return self.query_records(
            """
            SELECT s.id, s.kind, s.name, s.fqname, s.embedding,
                   s.line, s.file_id,
                   f.path AS file_path, f.project_id, f.is_test
            FROM symbols s JOIN files f ON s.file_id = f.id
            """
        )

    def get_symbols_for_impact(self, symbol_query: str, project_id: str | None = None) -> list[str]:
        """Resolve symbol query → matching symbol IDs — used by analyze_impact."""
        q = symbol_query.lower()
        if project_id:
            rows = self.query_records(
                """
                SELECT s.id FROM symbols s JOIN files f ON s.file_id = f.id
                WHERE f.project_id = ?
                  AND (s.id = ? OR lower(s.name) = ? OR lower(s.fqname) = ? OR lower(s.fqname) LIKE ?)
                LIMIT 50
                """,
                {"pid": project_id, "q1": symbol_query, "q2": q, "q3": q, "q4": f"%{q}%"},
            )
        else:
            rows = self.query_records(
                """
                SELECT s.id FROM symbols s
                WHERE s.id = ? OR lower(s.name) = ? OR lower(s.fqname) = ? OR lower(s.fqname) LIKE ?
                LIMIT 50
                """,
                {"q1": symbol_query, "q2": q, "q3": q, "q4": f"%{q}%"},
            )
        return [r["id"] for r in rows]

    def get_method_metadata_bulk(self, method_ids: list[str]) -> dict[str, dict]:
        """Bulk-resolve method IDs to metadata — used by analyze_impact."""
        if not method_ids:
            return {}
        ph = ", ".join("?" * len(method_ids))
        rows = self.query_records(
            f"""
            SELECT m.id, m.name, m.signature AS fqname,
                   c.fqcn AS class_fqcn, f.path AS file_path, f.project_id
            FROM methods m
            JOIN classes c ON m.class_id = c.id
            JOIN files f ON c.file_id = f.id
            WHERE m.id IN ({ph})
            """,
            {f"id{i}": v for i, v in enumerate(method_ids)},
        )
        return {r["id"]: r for r in rows}

    def get_callers_of(self, method_id: str) -> list[str]:
        rows = self.query_records("SELECT source_id FROM calls WHERE target_id = ?", {"id": method_id})
        return [r["source_id"] for r in rows]

    def get_callees_of(self, method_id: str) -> list[str]:
        rows = self.query_records("SELECT target_id FROM calls WHERE source_id = ?", {"id": method_id})
        return [r["target_id"] for r in rows]

    def get_all_call_edges(self, project_id: str | None = None) -> list[dict]:
        """Return all CALLS edges — used by community detection and flow analysis."""
        if project_id:
            return self.query_records(
                """
                SELECT c.source_id, c.target_id, c.confidence, c.reason
                FROM calls c
                JOIN methods src ON c.source_id = src.id
                JOIN classes cl ON src.class_id = cl.id
                JOIN files f ON cl.file_id = f.id
                WHERE f.project_id = ?
                """,
                {"pid": project_id},
            )
        return self.query_records("SELECT source_id, target_id, confidence, reason FROM calls")

    def get_all_methods(self, project_id: str | None = None) -> list[dict]:
        if project_id:
            return self.query_records(
                """
                SELECT m.id, m.class_id, m.name, m.signature, m.return_type,
                       m.modifiers, m.is_constructor, m.is_test,
                       c.fqcn AS class_fqcn, c.file_id,
                       f.path AS file_path, f.project_id
                FROM methods m
                JOIN classes c ON m.class_id = c.id
                JOIN files f ON c.file_id = f.id
                WHERE f.project_id = ?
                """,
                {"pid": project_id},
            )
        return self.query_records(
            """
            SELECT m.id, m.class_id, m.name, m.signature, m.return_type,
                   m.modifiers, m.is_constructor, m.is_test,
                   c.fqcn AS class_fqcn, c.file_id,
                   f.path AS file_path, f.project_id
            FROM methods m
            JOIN classes c ON m.class_id = c.id
            JOIN files f ON c.file_id = f.id
            """
        )

    def get_all_classes(self, project_id: str | None = None) -> list[dict]:
        if project_id:
            return self.query_records(
                """
                SELECT c.id, c.fqcn, c.name, c.package, c.file_id
                FROM classes c JOIN files f ON c.file_id = f.id
                WHERE f.project_id = ?
                """,
                {"pid": project_id},
            )
        return self.query_records("SELECT id, fqcn, name, package, file_id FROM classes")

    def get_dead_code_candidates(self, project_id: str | None = None) -> list[dict]:
        """Methods with no incoming CALLS — used by detect_dead_code."""
        if project_id:
            return self.query_records(
                """
                SELECT m.id, m.name, m.signature, m.is_constructor, m.is_test,
                       c.fqcn AS class_fqcn, f.path AS file_path, f.project_id
                FROM methods m
                JOIN classes c ON m.class_id = c.id
                JOIN files f ON c.file_id = f.id
                LEFT JOIN calls ca ON ca.target_id = m.id
                WHERE f.project_id = ? AND ca.source_id IS NULL
                  AND m.is_constructor = false
                """,
                {"pid": project_id},
            )
        return self.query_records(
            """
            SELECT m.id, m.name, m.signature, m.is_constructor, m.is_test,
                   c.fqcn AS class_fqcn, f.path AS file_path, f.project_id
            FROM methods m
            JOIN classes c ON m.class_id = c.id
            JOIN files f ON c.file_id = f.id
            LEFT JOIN calls ca ON ca.target_id = m.id
            WHERE ca.source_id IS NULL AND m.is_constructor = false
            """
        )

    # ------------------------------------------------------------------
    # Snapshot (same as GraphStore)
    # ------------------------------------------------------------------

    def snapshot_to_read_replica(self, background: bool = False) -> bool:
        src = self._db_path
        if not os.path.exists(src):
            return False
        if background:
            self._inst_snapshot_pending.set()
            inst = self

            def _worker() -> None:
                while inst._inst_snapshot_pending.is_set():
                    inst._inst_snapshot_pending.clear()
                    with inst._inst_snapshot_lock:
                        inst._do_snapshot()

            if not self._inst_snapshot_lock.locked():
                t = threading.Thread(target=_worker, daemon=True, name="codespine-duckdb-snapshot")
                t.start()
            return True
        with self._inst_snapshot_lock:
            return self._do_snapshot()

    def _do_snapshot(self) -> bool:
        src = self._db_path
        dst = self._snapshot_path
        if not os.path.exists(src):
            return False
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        tmp = dst + ".tmp"
        try:
            # Flush WAL → main file so the file copy captures all committed data.
            try:
                self._conn.execute("CHECKPOINT")
            except Exception as chk_exc:
                LOGGER.debug("CHECKPOINT before snapshot skipped: %s", chk_exc)
            if os.path.exists(tmp):
                os.remove(tmp)
            shutil.copy2(src, tmp)
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(tmp, dst)
            with open(dst + ".updated", "w") as f:
                f.write(str(int(time.time())))
            return True
        except Exception as exc:
            LOGGER.warning("DuckDB snapshot failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Rebuild / force delete
    # ------------------------------------------------------------------

    def rebuild_empty_db(self) -> None:
        self._conn.close()
        for p in [self._db_path, self._snapshot_path,
                  self._snapshot_path + ".tmp", self._snapshot_path + ".updated"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = duckdb.connect(self._db_path)
        self._ensure_schema()

    def force_delete_all_data(self) -> list[str]:
        removed = []
        for p in [self._db_path, self._snapshot_path,
                  self._snapshot_path + ".tmp", self._snapshot_path + ".updated"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                    removed.append(p)
                except OSError:
                    pass
        return removed
