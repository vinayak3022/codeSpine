"""Cross-module call edge linker.

After all modules in a workspace have been individually indexed, each module's
call resolver only sees methods *within that module* (the class/method catalogs
are project-scoped).  This module fills the gap by:

  1. Building a **global** class-name index across ALL projects.
  2. Scanning every method's signature and return type for class names that
     belong to a DIFFERENT project.
  3. Creating CALLS edges between the referencing method and the methods of
     the referenced class.

Two linking strategies are applied:

  Strategy A — Name + arity match  (confidence 0.7)
      The referencing method M_src calls a method with the same name AND
      parameter count as a method M_dst in the referenced class.  This catches
      delegation, interface-implementation forwarding, and adapter patterns.

  Strategy B — Type-reference fallback  (confidence 0.4)
      For every *public, non-constructor* method in the referenced class that
      received NO name-match edge, create ONE low-confidence edge from the
      referencing method.  This prevents methods that are genuinely used
      cross-module from appearing as dead code.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

LOGGER = logging.getLogger(__name__)

# Very short class names produce too many false-positive matches when scanned
# as substrings of method signatures.  Skip names ≤ this length.
_MIN_CLASS_NAME_LEN = 4

# Regex to split a Java signature into word tokens (class names, keywords, etc.)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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

    # ── 1. Global class index ─────────────────────────────────────────
    all_classes = store.query_records(
        """
        MATCH (c:Class), (f:File)
        WHERE c.file_id = f.id
        RETURN c.id as cid, c.name as name, c.fqcn as fqcn, f.project_id as pid
        """
    )

    # class_name → [(class_id, project_id)]
    name_to_classes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for c in all_classes:
        name_to_classes[c["name"]].append((c["cid"], c["pid"]))

    # ── 2. Per-project class name sets (for O(1) lookups) ─────────────
    # For each project pair (src, dst), we need the set of class names
    # that belong to the OTHER project(s).  Pre-compute per-project sets.
    classes_per_project: dict[str, set[str]] = defaultdict(set)
    for c in all_classes:
        if len(c["name"]) > _MIN_CLASS_NAME_LEN:
            classes_per_project[c["pid"]].add(c["name"])

    # ── 3. Scan methods for cross-project type references ─────────────
    new_edges = 0
    seen: set[tuple[str, str]] = set()

    for src_pid in project_ids:
        # Build the set of "interesting" class names from OTHER projects
        other_class_names: set[str] = set()
        for other_pid in project_ids:
            if other_pid != src_pid:
                other_class_names |= classes_per_project.get(other_pid, set())

        if not other_class_names:
            continue

        # Fetch all methods in this project
        src_methods = store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            RETURN m.id as mid, m.name as name, m.signature as sig,
                   m.return_type as rtype, c.id as cid
            """,
            {"pid": src_pid},
        )

        for sm in src_methods:
            sig = sm.get("sig") or ""
            rtype = sm.get("rtype") or ""
            # Tokenize signature + return type into words
            tokens = set(_TOKEN_RE.findall(sig + " " + rtype))
            # Find which class names from other projects appear in the tokens
            matched_class_names = tokens & other_class_names
            if not matched_class_names:
                continue

            # For each matched class, create CALLS edges
            for class_name in matched_class_names:
                for dst_cid, dst_pid in name_to_classes.get(class_name, []):
                    if dst_pid == src_pid:
                        continue  # same project — not cross-module

                    # Get methods of the destination class
                    dst_methods = store.query_records(
                        """MATCH (m:Method) WHERE m.class_id = $cid
                           RETURN m.id as mid, m.name as name, m.signature as sig,
                                  m.modifiers as modifiers, m.is_constructor as is_ctor""",
                        {"cid": dst_cid},
                    )
                    if not dst_methods:
                        continue

                    # Strategy A: name + arity match
                    matched_dst_mids: set[str] = set()
                    sm_name = sm["name"]
                    sm_pc = _param_count(sm.get("sig") or "")
                    for dm in dst_methods:
                        if dm["name"] == sm_name:
                            dm_pc = _param_count(dm.get("sig") or "")
                            if dm_pc == sm_pc:
                                pair = (sm["mid"], dm["mid"])
                                if pair not in seen:
                                    seen.add(pair)
                                    try:
                                        store.add_call(
                                            sm["mid"], dm["mid"],
                                            0.7, "cross_module_name_match",
                                        )
                                        new_edges += 1
                                    except Exception as exc:
                                        LOGGER.debug("Name-match edge failed: %s", exc)
                                matched_dst_mids.add(dm["mid"])

                    # Strategy B: fallback for unmatched public dst methods
                    for dm in dst_methods:
                        if dm["mid"] in matched_dst_mids:
                            continue
                        if dm.get("is_ctor"):
                            continue
                        mods = dm.get("modifiers") or []
                        mod_strs = {str(m).strip() for m in mods} if mods else set()
                        if "private" in mod_strs:
                            continue

                        pair = (sm["mid"], dm["mid"])
                        if pair in seen:
                            continue
                        seen.add(pair)
                        try:
                            store.add_call(
                                sm["mid"], dm["mid"],
                                0.4, "cross_module_type_ref",
                            )
                            new_edges += 1
                        except Exception as exc:
                            LOGGER.debug("Fallback edge failed: %s", exc)

    LOGGER.info("Cross-module linking: created %d new call edges.", new_edges)
    return new_edges
