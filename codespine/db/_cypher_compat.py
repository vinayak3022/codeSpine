"""Cypher-to-SQL translation for CodeSpine's DuckDB backend.

Translates the specific subset of OpenCypher used by CodeSpine into
equivalent DuckDB SQL so that every ``store.query_records(cypher, params)``
call continues to work without touching the call-sites.

Supported constructs
--------------------
- Node patterns            MATCH (alias:Label) or (a:L {prop: $v})
- Anonymous nodes          (:Label) in NOT-EXISTS subqueries
- Relationship patterns    (a)-[r:REL]->(b) or (a)-[:REL]->(b)
- Multi-hop patterns       (a)-[:R1]->(x)-[:R2]->(b)
- WHERE                    =, <>, IN, CONTAINS, lower(), coalesce(),
                           IS NULL, IS NOT NULL, >= , <=
- NOT EXISTS subqueries    NOT EXISTS { MATCH (:N)-[:R]->(m) }
- WITH … ORDER BY          Kuzu paging construct → plain ORDER BY
- DISTINCT, ORDER BY, LIMIT
- Aggregates               count(n) → count(*)
- Literal values           'string' in RETURN (e.g. 'method' as kind)
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Schema mappings
# ---------------------------------------------------------------------------

# Kuzu node label → DuckDB table name
_LABEL_TABLE: dict[str, str] = {
    "Project":   "projects",
    "File":      "files",
    "Class":     "classes",
    "Method":    "methods",
    "Symbol":    "symbols",
    "Community": "communities",
    "Flow":      "flows",
}

# Kuzu relationship type → (edge_table, src_col, dst_col, extra_where | None)
_REL_EDGE: dict[str, tuple[str, str, str, str | None]] = {
    "CALLS":           ("calls",             "source_id",    "target_id",    None),
    "OVERRIDES":       ("references_type",   "src_id",       "dst_id",       "rel = 'OVERRIDES'"),
    "IMPLEMENTS":      ("references_type",   "src_id",       "dst_id",       "rel = 'IMPLEMENTS'"),
    "INJECTS":         ("injects",           "src_class_id", "dst_class_id", None),
    "BINDS_INTERFACE": ("binds_interface",   "src_class_id", "dst_class_id", None),
    "IN_COMMUNITY":    ("community_members", "symbol_id",    "community_id", None),
    "IN_FLOW":         ("flow_members",      "symbol_id",    "flow_id",      None),
    "CO_CHANGED_WITH": ("co_changed_with",   "file_a",       "file_b",       None),
}


def is_cypher(query: str) -> bool:
    """Return True if *query* looks like Cypher rather than SQL."""
    q = query.lstrip()
    return bool(re.match(r"(?i)\s*MATCH\b", q))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def translate(cypher: str, params: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Translate *cypher* to DuckDB SQL.

    Returns ``(sql, params_dict)`` where *params_dict* preserves the original
    ``$name`` bindings so they can be passed directly to
    ``duckdb.connect().execute(sql, params_dict)``.
    """
    sql = _translate(cypher)
    return sql, (params or {})


# ---------------------------------------------------------------------------
# Internal translation pipeline
# ---------------------------------------------------------------------------

_ALL_EDGE_TABLES = (
    "calls",
    "references_type",
    "injects",
    "binds_interface",
    "community_members",
    "flow_members",
    "co_changed_with",
)


def _translate_anonymous_edge_count(cypher: str) -> str | None:
    """Handle `MATCH ()-[r]->() RETURN count(r) [as X]`.

    Anonymous edge patterns carry no labels, so the generic translator
    cannot derive a FROM table.  We special-case the count-all-edges
    pattern by unioning row-counts across every edge table in the
    schema, which is what CodeSpine actually asks for.
    """
    q = re.sub(r"\s+", " ", cypher.strip())
    # Accept MATCH ()-[r]->() RETURN count(r) [as alias]
    m = re.match(
        r"(?i)MATCH\s*\(\s*\)\s*-\s*\[\s*(\w+)?\s*\]\s*->\s*\(\s*\)\s*"
        r"RETURN\s+count\s*\(\s*\*?\w*\s*\)\s*(?:as\s+(\w+))?\s*$",
        q,
    )
    if not m:
        return None
    alias = m.group(2) or "count"
    unions = " UNION ALL ".join(
        f"SELECT COUNT(*) AS c FROM {tbl}" for tbl in _ALL_EDGE_TABLES
    )
    return f"SELECT COALESCE(SUM(c), 0) AS {alias} FROM ({unions}) t"


def _translate(cypher: str) -> str:
    # Fast-path: anonymous edge-count query used by `analyse` summary.
    special = _translate_anonymous_edge_count(cypher)
    if special is not None:
        return special

    q = re.sub(r"\s+", " ", cypher.strip())

    # Collect node aliases before we start mangling the string
    aliases: dict[str, str] = {}          # alias → table
    inline_conds: list[str] = []          # extra WHERE from {prop: $val}
    edge_from: list[str] = []             # edge-table aliases to add to FROM
    edge_conds: list[str] = []            # join conditions from rel patterns
    _rel_counter = {"n": 0}

    # ----------------------------------------------------------------
    # 1.  Collect named node patterns  (alias:Label) or (alias:Label {..})
    # ----------------------------------------------------------------
    node_re = re.compile(r"\((\w+):(\w+)(?:\s*\{([^}]+)\})?\)")
    for m in node_re.finditer(q):
        alias, label, inline = m.group(1), m.group(2), m.group(3)
        if label in _LABEL_TABLE and alias not in aliases:
            aliases[alias] = _LABEL_TABLE[label]
        if inline:
            for kv in re.finditer(r"(\w+)\s*:\s*(\$\w+)", inline):
                inline_conds.append(f"{alias}.{kv.group(1)} = {kv.group(2)}")

    # ----------------------------------------------------------------
    # 2.  Handle NOT EXISTS { MATCH (:N)-[:R]->(m) }
    #     → NOT EXISTS (SELECT 1 FROM edge_table WHERE dst_col = m.id)
    # ----------------------------------------------------------------
    def _replace_not_exists(m_obj: re.Match) -> str:
        inner = m_obj.group(1)
        # extract (:Label)-[:REL]->(alias) or (alias)-[:REL]->(:Label)
        rel_m = re.search(
            r"\((\w*):?(\w*)\)-\[:(\w+)\]->\((\w*):?(\w*)\)", inner
        )
        if not rel_m:
            return m_obj.group(0)
        src_alias = rel_m.group(1)
        rel_type  = rel_m.group(3)
        dst_alias = rel_m.group(4)
        if rel_type not in _REL_EDGE:
            return m_obj.group(0)
        edge_tbl, src_col, dst_col, extra = _REL_EDGE[rel_type]
        # Determine which side is the "anchor" (non-anonymous)
        if dst_alias and dst_alias in aliases:
            subq = (
                f"NOT EXISTS (SELECT 1 FROM {edge_tbl} _ne"
                f" WHERE _ne.{dst_col} = {dst_alias}.id)"
            )
        elif src_alias and src_alias in aliases:
            subq = (
                f"NOT EXISTS (SELECT 1 FROM {edge_tbl} _ne"
                f" WHERE _ne.{src_col} = {src_alias}.id)"
            )
        else:
            return m_obj.group(0)
        return subq

    q = re.sub(
        r"NOT\s+EXISTS\s*\{\s*MATCH\s*([^}]+)\}",
        _replace_not_exists,
        q,
        flags=re.IGNORECASE,
    )

    # ----------------------------------------------------------------
    # 3.  Collect and remove relationship patterns
    #     (a)-[r:REL]->(b)  or  (a)-[:REL]->(b)
    #     Multi-hop: (a)-[:R1]->(x)-[:R2]->(b) is processed left to right.
    # ----------------------------------------------------------------
    # We'll iteratively strip one hop at a time.
    rel_pattern = re.compile(
        r"\((\w+)(?::\w+(?:\s*\{[^}]*\})?)?\)"  # src node (already parsed)
        r"\s*-\[(\w*):(\w+)\]->\s*"
        r"\((\w+)(?::\w+(?:\s*\{[^}]*\})?)?\)"  # dst node
    )

    def _process_rel(m_obj: re.Match) -> str:
        src_alias = m_obj.group(1)
        rel_alias = m_obj.group(2)
        rel_type  = m_obj.group(3)
        dst_alias = m_obj.group(4)
        if rel_type not in _REL_EDGE:
            return m_obj.group(0)  # leave unknown rels untouched
        edge_tbl, src_col, dst_col, extra = _REL_EDGE[rel_type]
        _rel_counter["n"] += 1
        if not rel_alias:
            rel_alias = f"_r{_rel_counter['n']}"
        if rel_alias not in aliases:
            aliases[rel_alias] = edge_tbl
            edge_from.append(f"{edge_tbl} {rel_alias}")
            edge_conds.append(f"{rel_alias}.{src_col} = {src_alias}.id")
            edge_conds.append(f"{rel_alias}.{dst_col} = {dst_alias}.id")
            if extra:
                edge_conds.append(f"{rel_alias}.{extra}")
        # Return just the dst node so multi-hop chains resolve left→right
        return f"({dst_alias})"

    # Apply repeatedly until no more rel patterns remain
    prev = None
    while prev != q:
        prev = q
        q = rel_pattern.sub(_process_rel, q)

    # ----------------------------------------------------------------
    # 4.  Remove all MATCH … clauses (now that relationships are handled)
    #     We keep the text after the last MATCH so WHERE / RETURN survive.
    # ----------------------------------------------------------------
    # Remove "MATCH pattern [, pattern …]" segments
    # A MATCH clause ends at WHERE, RETURN, WITH, ORDER, LIMIT, or another MATCH.
    q = re.sub(
        r"(?i)\bMATCH\s+"
        r"(?:\([^)]*\)(?:\s*,\s*\([^)]*\))*)",
        "",
        q,
    ).strip()

    # ----------------------------------------------------------------
    # 5.  Split into clauses: WHERE / WITH … ORDER BY / RETURN / LIMIT
    # ----------------------------------------------------------------
    # Extract RETURN clause (everything after RETURN, before ORDER BY / LIMIT)
    ret_match = re.search(
        r"(?i)\bRETURN\b\s+(DISTINCT\s+)?(.*?)(?=\s*(?:ORDER\s+BY|LIMIT|$))",
        q,
    )
    ret_distinct = ""
    ret_cols = "*"
    if ret_match:
        ret_distinct = "DISTINCT " if ret_match.group(1) else ""
        ret_cols = ret_match.group(2).strip()
        q = q[: ret_match.start()].strip() + q[ret_match.end() :]

    # Extract ORDER BY
    order_match = re.search(r"(?i)\bORDER\s+BY\b(.+?)(?=\s*(?:LIMIT|$))", q)
    order_clause = ""
    if order_match:
        order_clause = "ORDER BY " + order_match.group(1).strip()
        q = q[: order_match.start()].strip() + q[order_match.end() :]

    # Extract LIMIT
    limit_match = re.search(r"(?i)\bLIMIT\s+(\$?\w+|\d+)", q)
    limit_clause = ""
    if limit_match:
        limit_clause = "LIMIT " + limit_match.group(1)
        q = q[: limit_match.start()].strip() + q[limit_match.end() :]

    # Extract WITH … ORDER BY (Kuzu paging: WITH m ORDER BY m.x LIMIT n)
    with_match = re.search(
        r"(?i)\bWITH\s+[\w, ]+\s+ORDER\s+BY\s+(.+?)(?=\s*(?:LIMIT|RETURN|$))", q
    )
    if with_match and not order_clause:
        order_clause = "ORDER BY " + with_match.group(1).strip()
        q = q[: with_match.start()].strip() + q[with_match.end() :]

    # Remove remaining WITH clauses
    q = re.sub(r"(?i)\bWITH\b[^R]*?(?=\bRETURN\b|$)", "", q).strip()

    # Extract WHERE clause
    where_match = re.search(r"(?i)\bWHERE\b(.+?)$", q, re.DOTALL)
    where_parts: list[str] = []
    if where_match:
        where_parts.append(where_match.group(1).strip())
        q = q[: where_match.start()].strip()

    # Add inline prop conditions and edge join conditions
    all_where = edge_conds + inline_conds + where_parts

    # ----------------------------------------------------------------
    # 6.  Build FROM clause from collected aliases + edge tables
    # ----------------------------------------------------------------
    from_parts = [f"{tbl} {alias}" for alias, tbl in aliases.items()
                  if tbl not in {et.split()[0] for et in edge_from}
                  or any(alias in ef for ef in edge_from)]
    # Deduplicate: edge_from entries are already included via aliases
    # Fallback: empty DuckDB-valid relation so an un-matched pattern
    # degrades to zero rows instead of crashing with "table dual missing".
    from_str = ", ".join(from_parts) if from_parts else "(SELECT 1 WHERE 1=0) _empty(x)"

    # ----------------------------------------------------------------
    # 7.  Transform WHERE conditions
    # ----------------------------------------------------------------
    where_str = " AND ".join(all_where) if all_where else ""
    where_str = _transform_where(where_str)

    # ----------------------------------------------------------------
    # 8.  Transform SELECT (RETURN → SELECT)
    # ----------------------------------------------------------------
    select_str = _transform_select(ret_cols)

    # ----------------------------------------------------------------
    # 9.  Assemble final SQL
    # ----------------------------------------------------------------
    parts = [f"SELECT {ret_distinct}{select_str}", f"FROM {from_str}"]
    if where_str:
        parts.append(f"WHERE {where_str}")
    if order_clause:
        parts.append(order_clause)
    if limit_clause:
        parts.append(limit_clause)

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Clause-level transformations
# ---------------------------------------------------------------------------

def _transform_where(where: str) -> str:
    if not where:
        return ""
    # CONTAINS → LIKE
    # LHS can be: column reference (alias.col), lower(...), or coalesce(...)
    where = re.sub(
        r"(\w+\.\w+|\blower\([^)]+\)|\bcoalesce\([^)]+\))\s+CONTAINS\s+(\$\w+|'[^']*')",
        lambda m: f"{m.group(1)} LIKE '%' || {m.group(2)} || '%'",
        where,
        flags=re.IGNORECASE,
    )
    # IN $list → = ANY($list)
    where = re.sub(
        r"\bIN\s+(\$\w+)\b",
        r"= ANY(\1)",
        where,
        flags=re.IGNORECASE,
    )
    # IN [literal list] stays as-is (DuckDB handles it)
    return where


def _transform_select(ret: str) -> str:
    if not ret:
        return "*"
    # count(alias) → count(*)
    ret = re.sub(r"\bcount\s*\(\s*\w+\s*\)", "count(*)", ret, flags=re.IGNORECASE)
    return ret
