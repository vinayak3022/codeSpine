"""Cypher-to-SQL translation for CodeSpine's DuckDB backend.

Translates the specific subset of OpenCypher used by CodeSpine into
equivalent DuckDB SQL so that every ``store.query_records(cypher, params)``
call continues to work without touching the call-sites.

Supported constructs
--------------------
- Node patterns            MATCH (alias:Label) or (a:L {prop: $v})
- Anonymous nodes          (:Label) in NOT-EXISTS subqueries
- Relationship patterns    (a)-[r:REL]->(b) directed
- Undirected edges         (a)-[r:REL]-(b)  → OR of both directions
- Virtual FK edges         (a)-[:HAS_METHOD]->(b) → b.class_id = a.id (no edge table)
- Multi-hop patterns       (a)-[:R1]->(x)-[:R2]->(b)
- Anonymous destination    (a)-[:CALLS]->()
- Multi-MATCH + WITH       Multiple MATCH clauses joined by WITH pipeline stages
- WHERE                    =, <>, IN, CONTAINS, lower(), coalesce(),
                           IS NULL, IS NOT NULL, >=, <=
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

# Virtual FK edges: backed by a foreign-key column rather than a separate
# edge table.  Format: (src_label_table, dst_label_table, dst_fk_col)
# e.g. HAS_METHOD: methods.class_id = class.id
_VIRTUAL_REL_EDGE: dict[str, tuple[str, str, str]] = {
    "HAS_METHOD": ("classes", "methods",  "class_id"),
    "HAS_CLASS":  ("files",   "classes",  "file_id"),
    "DECLARES":   ("files",   "symbols",  "file_id"),
}

# All real edge tables (used for anonymous total-count query)
_ALL_EDGE_TABLES = (
    "calls",
    "references_type",
    "injects",
    "binds_interface",
    "community_members",
    "flow_members",
    "co_changed_with",
)

# Top-level Cypher keywords recognised by the clause splitter.
# Order matters: longer / more-specific patterns must come before shorter ones.
_TOP_KEYWORDS = (
    "OPTIONAL MATCH",
    "ORDER BY",
    "MATCH",
    "WITH",
    "WHERE",
    "RETURN",
    "LIMIT",
)


def is_cypher(query: str) -> bool:
    """Return True if *query* looks like Cypher rather than SQL."""
    return bool(re.match(r"(?i)\s*MATCH\b", query.lstrip()))


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
# Clause splitter
# ---------------------------------------------------------------------------

def _split_clauses(q: str) -> list[tuple[str, str]]:
    """Tokenise a (whitespace-normalised) Cypher query into clause pairs.

    Returns a list of ``(keyword, body)`` tuples at the TOP level of the
    query.  Keywords inside ``()``, ``[]``, ``{}`` or quoted strings are
    NOT treated as clause boundaries.

    Example::

        "MATCH (f:File) WHERE f.id = $x WITH f MATCH (c:Class) RETURN c.name"
        →  [("MATCH", "(f:File)"),
            ("WHERE", "f.id = $x"),
            ("WITH",  "f"),
            ("MATCH", "(c:Class)"),
            ("RETURN","c.name")]
    """
    results: list[tuple[str, str]] = []
    n = len(q)
    i = 0
    depth_paren = depth_sq = depth_brace = 0
    in_quote = False
    quote_char = ""
    current_kw: str | None = None
    current_start = 0

    while i < n:
        ch = q[i]

        # ── Quote handling ────────────────────────────────────────────────
        if in_quote:
            if ch == "\\" and i + 1 < n:
                i += 2        # skip escaped char
                continue
            if ch == quote_char:
                in_quote = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_quote = True
            quote_char = ch
            i += 1
            continue

        # ── Depth tracking ────────────────────────────────────────────────
        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren = max(0, depth_paren - 1)
        elif ch == "[":
            depth_sq += 1
        elif ch == "]":
            depth_sq = max(0, depth_sq - 1)
        elif ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace = max(0, depth_brace - 1)

        # ── Keyword detection (top-level only) ───────────────────────────
        if depth_paren == 0 and depth_sq == 0 and depth_brace == 0:
            for kw in _TOP_KEYWORDS:
                kl = len(kw)
                if q[i : i + kl].upper() == kw:
                    if i > 0 and (q[i - 1].isalnum() or q[i - 1] in {"_", "$"}):
                        continue
                    end_pos = i + kl
                    # Require word boundary: end-of-string or non-word char
                    if end_pos < n and (q[end_pos].isalnum() or q[end_pos] == "_"):
                        continue     # e.g. "MATCHING" is not "MATCH"
                    # Flush previous clause
                    if current_kw is not None:
                        body = q[current_start:i].strip()
                        results.append((current_kw, body))
                    current_kw = kw
                    current_start = end_pos
                    i = end_pos
                    break
            else:
                i += 1
        else:
            i += 1

    # Flush final clause
    if current_kw is not None:
        body = q[current_start:].strip()
        results.append((current_kw, body))

    return results


# ---------------------------------------------------------------------------
# Internal translation pipeline
# ---------------------------------------------------------------------------

def _translate_anonymous_edge_count(cypher: str) -> str | None:
    """Fast-path for ``MATCH ()-[r]->() RETURN count(r) [as X]``.

    Anonymous edge patterns carry no labels so the generic translator
    cannot derive a FROM table.  We special-case the count-all-edges
    pattern by summing row-counts across every edge table.
    """
    q = re.sub(r"\s+", " ", cypher.strip())
    m = re.match(
        r"(?i)MATCH\s*\(\s*\)\s*-\s*\[\s*\w*\s*\]\s*->\s*\(\s*\)\s*"
        r"RETURN\s+count\s*\(\s*[*]?\w*\s*\)\s*(?:as\s+(\w+))?\s*$",
        q,
    )
    if not m:
        return None
    alias = m.group(1) or "count"
    unions = " UNION ALL ".join(
        f"SELECT COUNT(*) AS c FROM {tbl}" for tbl in _ALL_EDGE_TABLES
    )
    return f"SELECT COALESCE(SUM(c), 0) AS {alias} FROM ({unions}) t"


def _translate(cypher: str) -> str:
    # ── Fast-path: anonymous total-edge-count ────────────────────────────
    special = _translate_anonymous_edge_count(cypher)
    if special is not None:
        return special

    q = re.sub(r"\s+", " ", cypher.strip())
    clauses = _split_clauses(q)

    aliases: dict[str, str] = {}      # alias → table name
    inline_conds: list[str] = []      # WHERE from inline {prop: $val}
    edge_conds: list[str] = []        # join conditions from real edge tables
    virtual_conds: list[str] = []     # FK conditions from virtual edges
    where_parts: list[str] = []       # collected WHERE bodies
    ret_cols = "*"
    ret_distinct = ""
    order_clause = ""
    limit_clause = ""
    _rel_counter = {"n": 0}

    for kw, body in clauses:
        if kw == "MATCH":
            _absorb_match(body, aliases, inline_conds, edge_conds,
                          virtual_conds, _rel_counter)
        elif kw == "OPTIONAL MATCH":
            # Degenerate: register new node aliases so their columns are
            # selectable, but don't add INNER JOIN constraints.
            # Full LEFT JOIN support is a future enhancement.
            _absorb_match_nodes_only(body, aliases)
        elif kw == "WITH":
            # Paging idiom: WITH x ORDER BY x.col LIMIT n
            ob = re.search(r"(?i)ORDER\s+BY\s+(.+?)(?:\s+LIMIT\s+\S+)?\s*$", body)
            if ob and not order_clause:
                order_clause = "ORDER BY " + ob.group(1).strip()
            lm = re.search(r"(?i)LIMIT\s+(\S+)", body)
            if lm and not limit_clause:
                limit_clause = "LIMIT " + lm.group(1)
            # Pipeline-separator WITH (no ORDER BY) is simply dropped.
        elif kw == "WHERE":
            where_parts.append(body)
        elif kw == "RETURN":
            # DISTINCT
            dm = re.match(r"(?i)DISTINCT\s+(.*)", body)
            if dm:
                ret_distinct = "DISTINCT "
                body = dm.group(1)
            # Trailing ORDER BY inside RETURN
            ob = re.search(r"(?i)\s+ORDER\s+BY\s+(.+?)(?=\s*(?:LIMIT|$))", body)
            if ob and not order_clause:
                order_clause = "ORDER BY " + ob.group(1).strip()
                body = body[: ob.start()].strip()
            # Trailing LIMIT inside RETURN
            lm = re.search(r"(?i)\s+LIMIT\s+(\S+)", body)
            if lm and not limit_clause:
                limit_clause = "LIMIT " + lm.group(1)
                body = body[: lm.start()].strip()
            ret_cols = body.strip()
        elif kw == "ORDER BY":
            if not order_clause:
                order_clause = "ORDER BY " + body
        elif kw == "LIMIT":
            if not limit_clause:
                limit_clause = "LIMIT " + body.split()[0]

    # ── WHERE clause ──────────────────────────────────────────────────────
    all_conds: list[str] = []
    all_conds.extend(edge_conds)
    all_conds.extend(virtual_conds)
    all_conds.extend(inline_conds)
    for wp in where_parts:
        expanded = _expand_not_exists(wp, aliases)
        transformed = _transform_where(expanded)
        transformed, project_conds = _rewrite_project_id_refs(transformed, aliases)
        all_conds.extend(project_conds)
        if transformed:
            all_conds.append(transformed)
    where_str = " AND ".join(dict.fromkeys(c for c in all_conds if c))

    # ── SELECT ────────────────────────────────────────────────────────────
    select_str = _transform_select(ret_cols)
    select_str, select_project_conds = _rewrite_project_id_refs(select_str, aliases)
    if select_project_conds:
        where_str = " AND ".join(
            dict.fromkeys([where_str, *select_project_conds] if where_str else select_project_conds)
        )
    if order_clause:
        rewritten_order, order_project_conds = _rewrite_project_id_refs(order_clause, aliases)
        order_clause = rewritten_order
        if order_project_conds:
            where_str = " AND ".join(
                dict.fromkeys([where_str, *order_project_conds] if where_str else order_project_conds)
            )

    # ── FROM clause ───────────────────────────────────────────────────────
    # Built after WHERE/SELECT rewrites because project_id compatibility may
    # introduce synthetic joins from Method/Class/Symbol back to File.
    seen: set[str] = set()
    from_parts: list[str] = []
    for alias, tbl in aliases.items():
        entry = f"{tbl} {alias}"
        if entry not in seen:
            from_parts.append(entry)
            seen.add(entry)
    from_str = ", ".join(from_parts) if from_parts else "(SELECT 1 WHERE 1=0) _empty(x)"

    # ── Assemble ──────────────────────────────────────────────────────────
    parts = [f"SELECT {ret_distinct}{select_str}", f"FROM {from_str}"]
    if where_str:
        parts.append(f"WHERE {where_str}")
    if order_clause:
        parts.append(order_clause)
    if limit_clause:
        parts.append(limit_clause)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# MATCH body processing
# ---------------------------------------------------------------------------

# Rel pattern: (src)-[alias:TYPE]->(dst) or (src)-[alias:TYPE]-(dst)
# dst alias is optional (anonymous destination)
_REL_DIRECTED_RE = re.compile(
    r"\((\w+)(?::\w+(?:\s*\{[^}]*\})?)?\)"   # src node
    r"\s*-\[(\w*):(\w+)\]->\s*"               # -[alias:TYPE]->
    r"\((\w*)(?::\w+(?:\s*\{[^}]*\})?)?\)"    # dst node (alias optional)
)
_REL_UNDIRECTED_RE = re.compile(
    r"\((\w+)(?::\w+(?:\s*\{[^}]*\})?)?\)"   # src node
    r"\s*-\[(\w*):(\w+)\]-\s*"                # -[alias:TYPE]-
    r"\((\w*)(?::\w+(?:\s*\{[^}]*\})?)?\)"    # dst node
)
_NODE_RE = re.compile(r"\((\w+):(\w+)(?:\s*\{([^}]+)\})?\)")


def _absorb_match(
    body: str,
    aliases: dict[str, str],
    inline_conds: list[str],
    edge_conds: list[str],
    virtual_conds: list[str],
    _rel_counter: dict[str, int],
) -> None:
    """Extract node aliases and relationship patterns from one MATCH body."""

    # 1. Register named node patterns (alias:Label [{prop: $val}])
    for m in _NODE_RE.finditer(body):
        alias, label, inline = m.group(1), m.group(2), m.group(3)
        if label in _LABEL_TABLE and alias not in aliases:
            aliases[alias] = _LABEL_TABLE[label]
        if inline:
            for kv in re.finditer(r"(\w+)\s*:\s*(\$\w+)", inline):
                inline_conds.append(f"{alias}.{kv.group(1)} = {kv.group(2)}")

    # 2. Process directed rel patterns iteratively (handles multi-hop chains).
    def _do_directed(m_obj: re.Match) -> str:
        src, ralias, rtype, dst = (
            m_obj.group(1), m_obj.group(2), m_obj.group(3), m_obj.group(4)
        )
        _process_rel(src, ralias, rtype, dst,
                     aliases, edge_conds, virtual_conds, _rel_counter,
                     undirected=False)
        # Return just the dst node so multi-hop chains resolve left→right.
        return f"({dst})" if dst else "()"

    q = body
    prev = None
    while prev != q:
        prev = q
        q = _REL_DIRECTED_RE.sub(_do_directed, q)

    # 3. Process undirected rel patterns.
    for m in _REL_UNDIRECTED_RE.finditer(body):
        src, ralias, rtype, dst = (
            m.group(1), m.group(2), m.group(3), m.group(4)
        )
        _process_rel(src, ralias, rtype, dst,
                     aliases, edge_conds, virtual_conds, _rel_counter,
                     undirected=True)


def _absorb_match_nodes_only(body: str, aliases: dict[str, str]) -> None:
    """Register node aliases from an OPTIONAL MATCH without adding joins.

    This ensures columns from OPTIONAL MATCH nodes are reachable in SELECT/
    WHERE even though we don't yet emit a proper LEFT JOIN.
    """
    for m in _NODE_RE.finditer(body):
        alias, label = m.group(1), m.group(2)
        if label in _LABEL_TABLE and alias not in aliases:
            aliases[alias] = _LABEL_TABLE[label]


def _process_rel(
    src_alias: str,
    rel_alias: str,
    rel_type: str,
    dst_alias: str,
    aliases: dict[str, str],
    edge_conds: list[str],
    virtual_conds: list[str],
    _rel_counter: dict[str, int],
    *,
    undirected: bool,
) -> None:
    """Emit join conditions for one relationship hop."""

    # ── Virtual FK edge (no edge table) ──────────────────────────────────
    if rel_type in _VIRTUAL_REL_EDGE:
        _, dst_tbl, dst_fk_col = _VIRTUAL_REL_EDGE[rel_type]
        # Register dst alias if it carries a label (already done in
        # _absorb_match's node scan, but also handle un-labelled refs).
        if dst_alias and dst_alias not in aliases:
            aliases[dst_alias] = dst_tbl
        if dst_alias:
            virtual_conds.append(f"{dst_alias}.{dst_fk_col} = {src_alias}.id")
        return

    # ── Real edge table ───────────────────────────────────────────────────
    if rel_type not in _REL_EDGE:
        return     # unknown relationship type — skip silently

    edge_tbl, src_col, dst_col, extra = _REL_EDGE[rel_type]
    _rel_counter["n"] += 1
    ra = rel_alias or f"_r{_rel_counter['n']}"

    if ra not in aliases:
        aliases[ra] = edge_tbl
        edge_conds.append(f"{ra}.{src_col} = {src_alias}.id")
        if dst_alias:
            if undirected:
                # Undirected: match either direction.
                edge_conds.append(
                    f"({ra}.{dst_col} = {dst_alias}.id"
                    f" OR {ra}.{src_col} = {dst_alias}.id)"
                )
            else:
                edge_conds.append(f"{ra}.{dst_col} = {dst_alias}.id")
        if extra:
            edge_conds.append(f"{ra}.{extra}")


# ---------------------------------------------------------------------------
# WHERE expansion
# ---------------------------------------------------------------------------

def _expand_not_exists(body: str, aliases: dict[str, str]) -> str:
    """Replace ``NOT EXISTS { MATCH ... }`` with a SQL subquery."""

    def _replace(m_obj: re.Match) -> str:
        inner = m_obj.group(1)
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
        edge_tbl, src_col, dst_col, _ = _REL_EDGE[rel_type]
        if dst_alias and dst_alias in aliases:
            return (
                f"NOT EXISTS (SELECT 1 FROM {edge_tbl} _ne"
                f" WHERE _ne.{dst_col} = {dst_alias}.id)"
            )
        if src_alias and src_alias in aliases:
            return (
                f"NOT EXISTS (SELECT 1 FROM {edge_tbl} _ne"
                f" WHERE _ne.{src_col} = {src_alias}.id)"
            )
        return m_obj.group(0)

    return re.sub(
        r"NOT\s+EXISTS\s*\{\s*MATCH\s*([^}]+)\}",
        _replace,
        body,
        flags=re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Clause-level transformations
# ---------------------------------------------------------------------------

def _transform_where(where: str) -> str:
    if not where:
        return ""
    # CONTAINS → LIKE
    where = re.sub(
        r"(\w+\.\w+|\blower\([^)]+\)|\bcoalesce\([^)]+\))\s+CONTAINS\s+(\$\w+|'[^']*'|\blower\([^)]+\)|\bcoalesce\([^)]+\))",
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
    return where


def _rewrite_project_id_refs(text: str, aliases: dict[str, str]) -> tuple[str, list[str]]:
    """Map alias.project_id to the File table for labels without that column.

    Kuzu call-sites historically used ``m.project_id`` / ``c.project_id`` /
    ``s.project_id`` as a convenient graph property.  DuckDB normalizes project
    ownership through ``files.project_id``.  Rewriting here keeps existing tool
    queries valid without hand-editing every Cypher call-site.
    """
    if not text or "project_id" not in text:
        return text, []
    conds: list[str] = []

    def _replace(match: re.Match) -> str:
        alias = match.group(1)
        table = aliases.get(alias)
        if table == "files" or table is None:
            return match.group(0)
        if table == "methods":
            class_alias = f"_{alias}_project_class"
            file_alias = f"_{alias}_project_file"
            aliases.setdefault(class_alias, "classes")
            aliases.setdefault(file_alias, "files")
            conds.append(f"{alias}.class_id = {class_alias}.id")
            conds.append(f"{class_alias}.file_id = {file_alias}.id")
            return f"{file_alias}.project_id"
        if table in {"classes", "symbols"}:
            file_alias = f"_{alias}_project_file"
            aliases.setdefault(file_alias, "files")
            conds.append(f"{alias}.file_id = {file_alias}.id")
            return f"{file_alias}.project_id"
        return match.group(0)

    rewritten = re.sub(r"\b(\w+)\.project_id\b", _replace, text)
    deduped = list(dict.fromkeys(conds))
    return rewritten, deduped


def _transform_select(ret: str) -> str:
    if not ret:
        return "*"
    # count(alias) → count(*)
    ret = re.sub(r"\bcount\s*\(\s*\w+\s*\)", "count(*)", ret, flags=re.IGNORECASE)
    return ret
