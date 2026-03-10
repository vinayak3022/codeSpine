from __future__ import annotations

import logging
from typing import Iterable

LOGGER = logging.getLogger(__name__)


NODE_TABLES: list[tuple[str, str]] = [
    ("SchemaMeta", "CREATE NODE TABLE SchemaMeta(key STRING, value STRING, PRIMARY KEY (key))"),
    (
        "Project",
        "CREATE NODE TABLE Project(id STRING, path STRING, language STRING, indexed_at STRING, PRIMARY KEY (id))",
    ),
    (
        "File",
        "CREATE NODE TABLE File(id STRING, path STRING, project_id STRING, is_test BOOL, hash STRING, PRIMARY KEY (id))",
    ),
    (
        "Class",
        "CREATE NODE TABLE Class(id STRING, fqcn STRING, name STRING, package STRING, file_id STRING, PRIMARY KEY (id))",
    ),
    (
        "Method",
        "CREATE NODE TABLE Method(id STRING, class_id STRING, name STRING, signature STRING, return_type STRING, modifiers STRING[], is_constructor BOOL, is_test BOOL, PRIMARY KEY (id))",
    ),
    (
        "Symbol",
        "CREATE NODE TABLE Symbol(id STRING, kind STRING, name STRING, fqname STRING, file_id STRING, line INT64, col INT64, embedding FLOAT[384], PRIMARY KEY (id))",
    ),
    (
        "Community",
        "CREATE NODE TABLE Community(id STRING, label STRING, cohesion DOUBLE, PRIMARY KEY (id))",
    ),
    (
        "Flow",
        "CREATE NODE TABLE Flow(id STRING, entry_symbol_id STRING, kind STRING, PRIMARY KEY (id))",
    ),
]

REL_TABLES: Iterable[tuple[str, str]] = [
    ("DECLARES", "CREATE REL TABLE DECLARES(FROM File TO Symbol)"),
    ("HAS_METHOD", "CREATE REL TABLE HAS_METHOD(FROM Class TO Method)"),
    ("CALLS", "CREATE REL TABLE CALLS(FROM Method TO Method, confidence DOUBLE, reason STRING)"),
    ("REFERENCES_TYPE", "CREATE REL TABLE REFERENCES_TYPE(FROM Symbol TO Symbol, confidence DOUBLE)"),
    ("IMPLEMENTS", "CREATE REL TABLE IMPLEMENTS(FROM Class TO Class, confidence DOUBLE)"),
    ("OVERRIDES", "CREATE REL TABLE OVERRIDES(FROM Method TO Method, confidence DOUBLE)"),
    ("IN_COMMUNITY", "CREATE REL TABLE IN_COMMUNITY(FROM Symbol TO Community)"),
    ("IN_FLOW", "CREATE REL TABLE IN_FLOW(FROM Symbol TO Flow, depth INT64)"),
    (
        "CO_CHANGED_WITH",
        "CREATE REL TABLE CO_CHANGED_WITH(FROM File TO File, strength DOUBLE, cochanges INT64, months INT64)",
    ),
]


def _safe_execute(conn, query: str, params: dict | None = None) -> None:
    try:
        conn.execute(query, params or {})
    except Exception as exc:  # pragma: no cover - kuzu error surface varies by version
        LOGGER.debug("Ignoring schema query failure: %s (%s)", query, exc)


def ensure_schema(conn) -> None:
    for _, query in NODE_TABLES:
        _safe_execute(conn, query)

    for _, query in REL_TABLES:
        _safe_execute(conn, query)

    # Best-effort FTS/index hints. Kuzu versions differ, so keep optional.
    _safe_execute(
        conn,
        "CALL CREATE_FTS_INDEX('symbol_fts', 'Symbol', ['name', 'fqname'])",
    )
    _safe_execute(conn, "CALL CREATE_FTS_INDEX('method_fts', 'Method', ['name', 'signature'])")
    _safe_execute(conn, "CALL CREATE_FTS_INDEX('class_fts', 'Class', ['name', 'fqcn'])")

    # Best-effort migration: add indexed_at column to existing Project tables.
    _safe_execute(conn, "ALTER TABLE Project ADD indexed_at STRING DEFAULT ''")

    _safe_execute(
        conn,
        "MERGE (s:SchemaMeta {key: 'schema_version'}) SET s.value = '3'",
    )
