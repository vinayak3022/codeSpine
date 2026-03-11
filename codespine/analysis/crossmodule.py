"""Cross-module call edge linker.

After all modules in a workspace have been individually indexed, each module's
call resolver only sees methods within that module.  This module fills the gap
by scanning the graph for cross-project class references (REFERENCES_TYPE and
IMPLEMENTS edges) and creating CALLS edges between methods where the call is
plausible.

Strategy A — Name + arity match  (confidence 0.7)
    If src_class references dst_class (cross-project) and both have a method
    with the same name and same parameter count, create a CALLS edge.  This
    catches delegation, interface-implementation forwarding, and adapter
    patterns.

Strategy B — Type-reference fallback  (confidence 0.4)
    For each *public* method in dst_class that received NO name-match edge,
    create ONE low-confidence edge from a representative src method (preferring
    one with zero outgoing calls).  This prevents methods that are genuinely
    used cross-module from appearing as dead code.
"""
from __future__ import annotations

import logging
from collections import defaultdict

LOGGER = logging.getLogger(__name__)


def _param_count(sig: str) -> int:
    """Count parameters from a method signature string."""
    if not sig or "(" not in sig or ")" not in sig:
        return 0
    arg_str = sig[sig.find("(") + 1: sig.rfind(")")]
    return 0 if not arg_str.strip() else arg_str.count(",") + 1


def link_cross_module_calls(store, project_ids: list[str] | None = None) -> int:
    """Create CALLS edges between methods in different projects.

    Returns the number of new cross-module call edges created.
    """
    if project_ids is None:
        proj_recs = store.query_records("MATCH (p:Project) RETURN p.id as id")
        project_ids = [r["id"] for r in proj_recs]

    if len(project_ids) < 2:
        LOGGER.info(
            "Only %d project(s) indexed — skipping cross-module linking.",
            len(project_ids),
        )
        return 0

    # ── 1. Collect cross-project class pairs ──────────────────────────
    ref_pairs = store.query_records(
        """
        MATCH (src:Class)-[:REFERENCES_TYPE]->(dst:Class), (sf:File), (df:File)
        WHERE src.file_id = sf.id AND dst.file_id = df.id
          AND sf.project_id <> df.project_id
        RETURN DISTINCT src.id as src_cid, dst.id as dst_cid
        """
    )
    impl_pairs = store.query_records(
        """
        MATCH (src:Class)-[:IMPLEMENTS]->(dst:Class), (sf:File), (df:File)
        WHERE src.file_id = sf.id AND dst.file_id = df.id
          AND sf.project_id <> df.project_id
        RETURN DISTINCT src.id as src_cid, dst.id as dst_cid
        """
    )

    all_pairs: set[tuple[str, str]] = set()
    for p in ref_pairs:
        all_pairs.add((p["src_cid"], p["dst_cid"]))
    for p in impl_pairs:
        all_pairs.add((p["src_cid"], p["dst_cid"]))

    if not all_pairs:
        LOGGER.info("No cross-project class references found.")
        return 0

    LOGGER.info(
        "Cross-module: %d cross-project class pair(s) to process.",
        len(all_pairs),
    )

    # ── 2. Process each class pair ────────────────────────────────────
    new_edges = 0
    seen: set[tuple[str, str]] = set()

    for src_cid, dst_cid in all_pairs:
        src_methods = store.query_records(
            """MATCH (m:Method) WHERE m.class_id = $cid
               RETURN m.id as mid, m.name as name, m.signature as sig""",
            {"cid": src_cid},
        )
        dst_methods = store.query_records(
            """MATCH (m:Method) WHERE m.class_id = $cid
               RETURN m.id as mid, m.name as name, m.signature as sig,
                      m.modifiers as modifiers, m.is_constructor as is_ctor""",
            {"cid": dst_cid},
        )
        if not src_methods or not dst_methods:
            continue

        # Build name → methods index for src class
        src_by_name: dict[str, list[dict]] = defaultdict(list)
        for sm in src_methods:
            src_by_name[sm["name"]].append(sm)

        # ── Strategy A: name + arity matching ─────────────────────────
        matched_dst_mids: set[str] = set()

        for dm in dst_methods:
            dm_name = dm["name"]
            dm_pc = _param_count(dm.get("sig") or "")
            candidates = src_by_name.get(dm_name, [])
            for sm in candidates:
                sm_pc = _param_count(sm.get("sig") or "")
                if sm_pc == dm_pc:
                    pair = (sm["mid"], dm["mid"])
                    if pair in seen:
                        matched_dst_mids.add(dm["mid"])
                        continue
                    seen.add(pair)
                    try:
                        store.add_call(
                            sm["mid"], dm["mid"], 0.7, "cross_module_name_match",
                        )
                        new_edges += 1
                        matched_dst_mids.add(dm["mid"])
                    except Exception as exc:
                        LOGGER.debug("Name-match edge failed: %s", exc)

        # ── Strategy B: fallback for unmatched public dst methods ─────
        # Find a representative caller: prefer src methods with 0 outgoing calls
        fallback_src = None
        for sm in src_methods:
            out = store.query_records(
                "MATCH (m:Method {id: $mid})-[:CALLS]->(:Method) RETURN count(*) as n",
                {"mid": sm["mid"]},
            )
            if out and out[0]["n"] == 0:
                fallback_src = sm
                break
        if fallback_src is None and src_methods:
            fallback_src = src_methods[0]

        if fallback_src:
            for dm in dst_methods:
                if dm["mid"] in matched_dst_mids:
                    continue
                # Skip constructors and private methods
                if dm.get("is_ctor"):
                    continue
                mods = dm.get("modifiers") or []
                mod_strs = {str(m).strip() for m in mods} if mods else set()
                if "private" in mod_strs:
                    continue

                pair = (fallback_src["mid"], dm["mid"])
                if pair in seen:
                    continue
                seen.add(pair)
                try:
                    store.add_call(
                        fallback_src["mid"], dm["mid"], 0.4, "cross_module_type_ref",
                    )
                    new_edges += 1
                except Exception as exc:
                    LOGGER.debug("Fallback edge failed: %s", exc)

    LOGGER.info("Cross-module linking: created %d new call edges.", new_edges)
    return new_edges
