"""Tests for codespine.db._cypher_compat — Cypher→SQL translator."""

from __future__ import annotations

import pytest

from codespine.db._cypher_compat import is_cypher, translate, _translate


# ---------------------------------------------------------------------------
# is_cypher
# ---------------------------------------------------------------------------


def test_is_cypher_match_keyword():
    assert is_cypher("MATCH (n:File) RETURN n.path") is True


def test_is_cypher_lowercase():
    assert is_cypher("match (n:File) return n") is True


def test_is_cypher_leading_whitespace():
    assert is_cypher("  \n  MATCH (n:Method) RETURN n") is True


def test_is_cypher_plain_sql():
    assert is_cypher("SELECT * FROM files") is False


def test_is_cypher_empty_string():
    assert is_cypher("") is False


# ---------------------------------------------------------------------------
# translate() pass-through / params
# ---------------------------------------------------------------------------


def test_translate_returns_tuple():
    sql, params = translate("MATCH (n:File) RETURN n.path", {"x": 1})
    assert isinstance(sql, str)
    assert params == {"x": 1}


def test_translate_none_params():
    sql, params = translate("MATCH (n:File) RETURN n.path", None)
    assert params == {}


# ---------------------------------------------------------------------------
# Simple node-only MATCH
# ---------------------------------------------------------------------------


def test_simple_node_match():
    sql = _translate("MATCH (n:Method) RETURN n.name")
    assert "FROM" in sql
    assert "methods" in sql
    assert "n.name" in sql


def test_node_with_inline_prop():
    sql = _translate("MATCH (n:Method {name: $nm}) RETURN n.id")
    assert "n.name = $nm" in sql
    assert "methods" in sql


def test_multiple_nodes():
    sql = _translate("MATCH (f:File), (c:Class) RETURN f.path, c.name")
    assert "files" in sql
    assert "classes" in sql


# ---------------------------------------------------------------------------
# WHERE clause preservation
# ---------------------------------------------------------------------------


def test_where_equality():
    sql = _translate("MATCH (n:Method) WHERE n.project_id = $pid RETURN n.name")
    assert "n.project_id = $pid" in sql
    assert "WHERE" in sql


def test_where_contains_becomes_like():
    sql = _translate("MATCH (n:Method) WHERE n.name CONTAINS $q RETURN n.name")
    assert "LIKE" in sql
    assert "$q" in sql
    assert "CONTAINS" not in sql


def test_where_in_list_becomes_any():
    sql = _translate("MATCH (n:Method) WHERE n.id IN $ids RETURN n.id")
    assert "= ANY($ids)" in sql
    assert " IN " not in sql


def test_where_is_null():
    sql = _translate("MATCH (n:Symbol) WHERE n.embedding IS NULL RETURN n.id")
    assert "IS NULL" in sql


def test_where_is_not_null():
    sql = _translate("MATCH (n:Symbol) WHERE n.embedding IS NOT NULL RETURN n.id")
    assert "IS NOT NULL" in sql


# ---------------------------------------------------------------------------
# Relationship patterns
# ---------------------------------------------------------------------------


def test_calls_rel():
    sql = _translate(
        "MATCH (a:Method)-[:CALLS]->(b:Method) "
        "WHERE a.project_id = $pid RETURN b.id"
    )
    assert "calls" in sql
    assert "source_id" in sql
    assert "target_id" in sql


def test_rel_with_named_alias():
    sql = _translate(
        "MATCH (a:Method)-[r:CALLS]->(b:Method) "
        "RETURN a.id, b.id"
    )
    assert "calls" in sql
    assert "r.source_id" in sql or "source_id" in sql


def test_overrides_rel():
    sql = _translate(
        "MATCH (a:Method)-[:OVERRIDES]->(b:Method) RETURN a.id"
    )
    assert "references_type" in sql
    assert "OVERRIDES" in sql   # from the extra-where fragment


def test_in_community_rel():
    sql = _translate(
        "MATCH (s:Symbol)-[:IN_COMMUNITY]->(c:Community) "
        "WHERE c.id = $cid RETURN s.id"
    )
    assert "community_members" in sql
    assert "symbol_id" in sql
    assert "community_id" in sql


def test_injects_rel():
    sql = _translate(
        "MATCH (a:Class)-[:INJECTS]->(b:Class) RETURN a.id"
    )
    assert "injects" in sql
    assert "src_class_id" in sql


# ---------------------------------------------------------------------------
# Multi-hop patterns
# ---------------------------------------------------------------------------


def test_multi_hop():
    sql = _translate(
        "MATCH (m:Method)-[:CALLS]->(x:Method)-[:CALLS]->(y:Method) "
        "WHERE m.id = $mid RETURN y.id"
    )
    # Both hops must have been resolved
    assert sql.count("calls") >= 2
    assert "m.id = $mid" in sql


# ---------------------------------------------------------------------------
# NOT EXISTS subquery
# ---------------------------------------------------------------------------


def test_not_exists():
    sql = _translate(
        "MATCH (m:Method) "
        "WHERE NOT EXISTS { MATCH (:Symbol)-[:IN_COMMUNITY]->(m) } "
        "RETURN m.id"
    )
    assert "NOT EXISTS" in sql
    assert "SELECT 1 FROM" in sql
    assert "community_members" in sql


# ---------------------------------------------------------------------------
# DISTINCT, ORDER BY, LIMIT
# ---------------------------------------------------------------------------


def test_distinct():
    sql = _translate("MATCH (n:File) RETURN DISTINCT n.project_id")
    assert "DISTINCT" in sql


def test_order_by():
    sql = _translate("MATCH (n:Method) RETURN n.name ORDER BY n.name")
    assert "ORDER BY" in sql
    assert "n.name" in sql


def test_limit():
    sql = _translate("MATCH (n:Method) RETURN n.id LIMIT 10")
    assert "LIMIT 10" in sql


def test_limit_param():
    sql = _translate("MATCH (n:Method) RETURN n.id LIMIT $lim")
    assert "LIMIT $lim" in sql


# ---------------------------------------------------------------------------
# WITH … ORDER BY (Kuzu paging construct)
# ---------------------------------------------------------------------------


def test_with_order_by_paging():
    sql = _translate(
        "MATCH (n:Method) "
        "WITH n ORDER BY n.name "
        "RETURN n.name LIMIT 20"
    )
    assert "ORDER BY" in sql
    assert "n.name" in sql


# ---------------------------------------------------------------------------
# count() aggregate
# ---------------------------------------------------------------------------


def test_count_alias_becomes_star():
    sql = _translate("MATCH (n:Method) RETURN count(n)")
    assert "count(*)" in sql.lower()


def test_count_case_insensitive():
    sql = _translate("MATCH (n:Method) RETURN COUNT(n)")
    assert "count(*)" in sql.lower()


# ---------------------------------------------------------------------------
# Literal string in RETURN
# ---------------------------------------------------------------------------


def test_literal_string_in_return():
    sql = _translate("MATCH (n:Method) RETURN n.id, 'method' as kind")
    assert "'method'" in sql
    assert "kind" in sql


# ---------------------------------------------------------------------------
# lower() and coalesce() in WHERE
# ---------------------------------------------------------------------------


def test_lower_in_where():
    sql = _translate(
        "MATCH (n:Symbol) WHERE lower(n.name) CONTAINS $q RETURN n.id"
    )
    assert "LIKE" in sql


def test_coalesce_contains():
    sql = _translate(
        "MATCH (n:Symbol) "
        "WHERE coalesce(n.name, '') CONTAINS $q "
        "RETURN n.id"
    )
    assert "LIKE" in sql


# ---------------------------------------------------------------------------
# project_id scoping (common pattern used throughout server.py)
# ---------------------------------------------------------------------------


def test_project_scope():
    sql = _translate(
        "MATCH (n:Method) "
        "WHERE n.project_id = $pid "
        "RETURN n.id, n.name, n.signature"
    )
    assert "methods" in sql
    assert "n.project_id = $pid" in sql
    assert "n.id" in sql and "n.name" in sql and "n.signature" in sql


# ---------------------------------------------------------------------------
# SELECT * fallback when no RETURN
# ---------------------------------------------------------------------------


def test_no_return_gives_star():
    # Edge case: no RETURN clause — should still produce valid SQL
    sql = _translate("MATCH (n:File)")
    assert "SELECT" in sql
    assert "files" in sql


# ---------------------------------------------------------------------------
# Anonymous edge-count pattern (v1.0.5 regression)
# ---------------------------------------------------------------------------


def test_anonymous_edge_count():
    """`MATCH ()-[r]->() RETURN count(r) as count` — the query used by
    `codespine analyse` to report total edges — must translate to a
    DuckDB-valid query instead of falling through to `FROM dual`.
    """
    sql = _translate("MATCH ()-[r]->() RETURN count(r) as count")
    # Must reference real edge tables, not the Oracle-style `dual`.
    assert "dual" not in sql.lower()
    assert "calls" in sql
    assert "references_type" in sql
    assert "injects" in sql
    assert "binds_interface" in sql
    assert "community_members" in sql
    assert "flow_members" in sql
    assert "co_changed_with" in sql
    # Alias should survive so callers can read row["count"].
    assert "AS count" in sql or "as count" in sql


def test_anonymous_edge_count_no_alias():
    sql = _translate("MATCH ()-[r]->() RETURN count(r)")
    assert "dual" not in sql.lower()
    assert "calls" in sql


def test_anonymous_edge_count_unnamed_rel():
    # `MATCH ()-[]->()` (no rel variable) should also be handled
    sql = _translate("MATCH ()-[]->() RETURN count(*) as count")
    assert "dual" not in sql.lower()
    assert "calls" in sql


def test_unmatched_pattern_uses_safe_fallback():
    """If translator can't derive a FROM table, emit an empty DuckDB
    relation rather than Oracle's `dual`.
    """
    sql = _translate("MATCH (x:NotARealLabel) RETURN x.id")
    assert "dual" not in sql.lower()
