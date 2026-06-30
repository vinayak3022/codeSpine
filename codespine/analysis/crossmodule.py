"""Cross-module and cross-project call edge linker.

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

  Strategy B — Direct parameter/return type reference  (confidence 0.6)
      When the referenced class name appears directly as a parameter type or
      return type of the source method, create an edge to the class's
      constructor (if any).  This catches model/DTO/context instantiation.
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


def link_cross_module_calls(store, project_ids: list[str] | None = None, progress=None) -> int:
    """Create CALLS edges between methods in different projects.

    Returns the number of new cross-module call edges created.
    *progress* is an optional ``(status_str) -> None`` callback for live updates.
    """
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)
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

    _ping(f"building class index ({len(all_classes)} classes)")

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

    # ── 3. Pre-load all destination-class methods in ONE bulk query ───────
    # Collect every class ID that belongs to a project OTHER than its own so
    # we can load their methods in one round-trip instead of one per class.
    all_cross_cids: set[str] = set()
    for c in all_classes:
        if len(c["name"]) > _MIN_CLASS_NAME_LEN:
            all_cross_cids.add(c["cid"])

    _ping(f"loading methods for {len(all_cross_cids)} cross-module classes")
    dst_methods_by_cid: dict[str, list[dict]] = defaultdict(list)
    if all_cross_cids:
        bulk = store.query_records(
            """
            MATCH (m:Method)
            WHERE m.class_id IN $cids
            RETURN m.id as mid, m.name as name, m.signature as sig,
                   m.modifiers as modifiers, m.is_constructor as is_ctor,
                   m.class_id as cid
            """,
            {"cids": list(all_cross_cids)},
        )
        for dm in bulk:
            dst_methods_by_cid[dm["cid"]].append(dm)

    # ── 4. Scan methods for cross-project type references ─────────────
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

        _ping(f"scanning {src_pid} methods")

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

            # For each matched class, create CALLS edges using pre-loaded methods.
            for class_name in matched_class_names:
                for dst_cid, dst_pid in name_to_classes.get(class_name, []):
                    if dst_pid == src_pid:
                        continue  # same project — not cross-module

                    dst_methods = dst_methods_by_cid.get(dst_cid)
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

                    # Strategy B: if the referenced class name appears directly
                    # in the source method's parameter types or return type,
                    # link to the class's constructor (model/DTO instantiation).
                    if not matched_dst_mids:
                        rtype_tokens = set(_TOKEN_RE.findall(rtype))
                        sig_tokens = set(_TOKEN_RE.findall(sig))
                        if class_name in rtype_tokens or class_name in sig_tokens:
                            for dm in dst_methods:
                                if not dm.get("is_ctor"):
                                    continue
                                pair = (sm["mid"], dm["mid"])
                                if pair in seen:
                                    continue
                                seen.add(pair)
                                try:
                                    store.add_call(
                                        sm["mid"], dm["mid"],
                                        0.6, "cross_module_ctor_ref",
                                    )
                                    new_edges += 1
                                except Exception as exc:
                                    LOGGER.debug("Ctor-ref edge failed: %s", exc)

    _ping(f"{new_edges} edges created")
    LOGGER.info("Cross-module linking: created %d new call edges.", new_edges)
    return new_edges


def link_cross_project_calls(sg, progress=None) -> int:
    """Create CALLS edges between methods in *independently-indexed* projects.

    Unlike ``link_cross_module_calls`` which links within a multi-module
    workspace, this function scans ALL projects in the ShardedGraphStore and
    creates edges between methods whose class names appear in each other's
    signatures / return types across project boundaries.

    Edges are written to the shard that owns the *source* method's project so
    that consistent-hash locality is preserved.

    Returns the number of new edges created.
    """
    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    # 1. Fetch all project IDs across all shards via fan-out query.
    proj_recs = sg.query_records("MATCH (p:Project) RETURN p.id as id, p.path as path")
    project_ids = [r["id"] for r in proj_recs]
    if len(project_ids) < 2:
        LOGGER.info("Only %d project(s) indexed — skipping cross-project linking.", len(project_ids))
        return 0

    _ping(f"cross-project linking across {len(project_ids)} projects")

    # 2. Global class index (all classes across all shards).
    all_classes = sg.query_records("""
        MATCH (c:Class), (f:File)
        WHERE c.file_id = f.id
        RETURN c.id as cid, c.name as name, c.fqcn as fqcn, f.project_id as pid
    """)
    _ping(f"building class index ({len(all_classes)} classes)")

    name_to_classes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    classes_per_project: dict[str, set[str]] = defaultdict(set)
    for c in all_classes:
        name_to_classes[c["name"]].append((c["cid"], c["pid"]))
        if len(c["name"]) > _MIN_CLASS_NAME_LEN:
            classes_per_project[c["pid"]].add(c["name"])

    # 3. Pre-load all destination-class methods in one bulk query.
    all_cross_cids: set[str] = {c["cid"] for c in all_classes if len(c["name"]) > _MIN_CLASS_NAME_LEN}
    _ping(f"loading methods for {len(all_cross_cids)} cross-project classes")
    dst_methods_by_cid: dict[str, list[dict]] = defaultdict(list)
    if all_cross_cids:
        bulk = sg.query_records("""
            MATCH (m:Method), (c:Class), (f:File)
            WHERE c.id IN $cids AND m.class_id = c.id AND c.file_id = f.id
            RETURN m.id as mid, m.name as name, m.signature as sig,
                   m.modifiers as modifiers, m.is_constructor as is_ctor,
                   m.class_id as cid, f.project_id as pid
        """, {"cids": list(all_cross_cids)})
        for dm in bulk:
            dst_methods_by_cid[dm["cid"]].append(dm)

    # 4. Scan methods per project for cross-project type references.
    new_edges = 0
    seen: set[tuple[str, str]] = set()
    # Batch edges by source project for shard-local writes.
    edges_by_project: dict[str, list[dict]] = defaultdict(list)

    for src_pid in project_ids:
        other_class_names: set[str] = set()
        for other_pid in project_ids:
            if other_pid != src_pid:
                other_class_names |= classes_per_project.get(other_pid, set())
        if not other_class_names:
            continue

        _ping(f"scanning {src_pid} methods")
        src_methods = sg.query_records("""
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            RETURN m.id as mid, m.name as name, m.signature as sig,
                   m.return_type as rtype, c.id as cid
        """, {"pid": src_pid})

        for sm in src_methods:
            sig = sm.get("sig") or ""
            rtype = sm.get("rtype") or ""
            tokens = set(_TOKEN_RE.findall(sig + " " + rtype))
            matched_class_names = tokens & other_class_names
            if not matched_class_names:
                continue

            for class_name in matched_class_names:
                for dst_cid, dst_pid in name_to_classes.get(class_name, []):
                    if dst_pid == src_pid:
                        continue

                    dst_methods = dst_methods_by_cid.get(dst_cid)
                    if not dst_methods:
                        continue

                    # Strategy A: name + arity match
                    sm_name = sm["name"]
                    sm_pc = _param_count(sm.get("sig") or "")
                    matched_dst_mids: set[str] = set()
                    for dm in dst_methods:
                        if dm["name"] == sm_name and _param_count(dm.get("sig") or "") == sm_pc:
                            pair = (sm["mid"], dm["mid"])
                            if pair not in seen:
                                seen.add(pair)
                                edges_by_project[src_pid].append({
                                    "source_id": sm["mid"],
                                    "target_id": dm["mid"],
                                    "confidence": 0.7,
                                    "reason": "cross_project_name_match",
                                })
                                new_edges += 1
                            matched_dst_mids.add(dm["mid"])

                    # Strategy B: constructor ref for parameter/return type
                    if not matched_dst_mids:
                        rtype_tokens = set(_TOKEN_RE.findall(rtype))
                        sig_tokens = set(_TOKEN_RE.findall(sig))
                        if class_name in rtype_tokens or class_name in sig_tokens:
                            for dm in dst_methods:
                                if not dm.get("is_ctor"):
                                    continue
                                pair = (sm["mid"], dm["mid"])
                                if pair in seen:
                                    continue
                                seen.add(pair)
                                edges_by_project[src_pid].append({
                                    "source_id": sm["mid"],
                                    "target_id": dm["mid"],
                                    "confidence": 0.6,
                                    "reason": "cross_project_ctor_ref",
                                })
                                new_edges += 1

    # 5. Write edges to each source project's shard.
    for project_id, records in edges_by_project.items():
        try:
            shard_store = sg.shard(project_id)
            shard_store.add_calls_batch(records)
        except Exception as exc:
            LOGGER.warning("Failed to write %d cross-project edges for %s: %s",
                           len(records), project_id, exc)

    _ping(f"{new_edges} cross-project edges created")
    LOGGER.info("Cross-project linking: created %d new call edges.", new_edges)
    return new_edges


def link_dependency_imports(store, project_ids: list[str] | None = None, progress=None) -> int:
    """Create ``REFERENCES_TYPE`` edges from file-level import declarations.

    Reads cached import data from the meta-cache (stored during indexing) and
    creates ``REFERENCES_TYPE`` edges between symbols whose fully-qualified
    names appear in another project's import statements.

    This is distinct from ``link_cross_module_calls`` / ``link_cross_project_calls``
    which scan method signatures for type references.  This pass uses the
    explicit import declarations that Java/C#/Kotlin files already contain.

    Returns the number of new reference edges created.
    """
    import os
    import json

    from codespine.project_state import list_project_states
    from codespine.indexer.engine import JavaIndexer

    def _ping(msg: str) -> None:
        if progress:
            progress(msg)

    if project_ids is None:
        proj_recs = store.query_records("MATCH (p:Project) RETURN p.id as id")
        project_ids = [r["id"] for r in proj_recs]

    # Gather all indexed project IDs for cross-project resolution
    all_project_ids: list[str] = []
    try:
        proj_recs = store.query_records("MATCH (p:Project) RETURN p.id as id")
        all_project_ids = [r["id"] for r in proj_recs]
    except Exception:
        all_project_ids = list(project_ids)

    # We need at least 2 unique projects across the whole index
    unique_projects = set(all_project_ids)
    if len(unique_projects) < 2:
        LOGGER.info("link_dependency_imports: fewer than 2 projects (%d), skipping.", len(unique_projects))
        return 0

    _ping(f"import-resolution linking scanning {len(project_ids)} project(s) across {len(unique_projects)} total")

    # 1. Build symbol index: unqualified-name → [(qualified-name, symbol-id, project-id)]
    # Also build file_id → first symbol in that file (the source)
    symbol_index: dict[str, list[tuple[str, str, str]]] = {}
    file_to_symbols: dict[str, list[dict]] = {}
    sym_recs = store.query_records(
        """
        MATCH (s:Symbol), (f:File)
        WHERE s.file_id = f.id
        RETURN s.id as id, s.name as name, s.fqname as fqname, f.project_id as pid,
               f.id as file_id
        """
    )
    for sr in sym_recs:
        sname = str(sr.get("name") or "").strip()
        if sname:
            symbol_index.setdefault(sname.lower(), []).append(
                (str(sr.get("fqname") or ""), str(sr.get("id") or ""), str(sr.get("pid") or ""))
            )
        fid = str(sr.get("file_id") or "")
        if fid:
            file_to_symbols.setdefault(fid, []).append(sr)

    # 2. Scan each project's meta-cache for file-level imports.
    new_edges = 0
    seen: set[tuple[str, str]] = set()
    batch: list[dict] = []

    for pid in project_ids:
        meta_path = JavaIndexer._meta_cache_path(pid)
        if not os.path.isfile(meta_path):
            LOGGER.debug("link_dependency_imports: no meta-cache for %s at %s", pid, meta_path)
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception as exc:
            LOGGER.debug("link_dependency_imports: failed to read meta-cache %s: %s", meta_path, exc)
            continue

        if not isinstance(meta, dict):
            continue

        for file_id, file_data in meta.items():
            imports: list[str] = []
            if isinstance(file_data, dict):
                imports = file_data.get("imports") or []
            elif isinstance(file_data, list):
                imports = file_data
            if not imports:
                continue

            # Find the source symbol(s) for this file: first class symbol in the file
            source_sym_id = None
            for sym in file_to_symbols.get(file_id, []):
                if sym.get("kind") in ("class", "interface", "enum"):
                    source_sym_id = sym.get("id")
                    break
            if not source_sym_id:
                # fallback: any symbol in the file
                src_syms = file_to_symbols.get(file_id, [])
                if src_syms:
                    source_sym_id = src_syms[0].get("id")

            if not source_sym_id:
                LOGGER.debug("link_dependency_imports: no source symbol for file %s", file_id)
                continue

            for imp_fqn in imports:
                imp_name = imp_fqn.split(".")[-1].lower() if "." in imp_fqn else imp_fqn.lower()
                candidates = symbol_index.get(imp_name, [])
                if not candidates:
                    continue
                for dst_fqname, dst_sym_id, dst_pid in candidates:
                    if dst_pid == pid or not dst_pid:
                        continue  # same project or unknown project
                    pair = (source_sym_id, dst_sym_id)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    batch.append({
                        "src_id": source_sym_id,
                        "dst_id": dst_sym_id,
                        "rel": "REFERENCES_TYPE",
                        "confidence": 0.9,
                    })
                    new_edges += 1

    # 3. Write edges — batch by source-project shard
    if batch:
        # Group edges by source project ID (infer from symbol_index)
        src_pid_map: dict[str, str] = {}
        for sr in sym_recs:
            src_pid_map[str(sr.get("id") or "")] = str(sr.get("pid") or "")
        edges_by_project: dict[str, list[dict]] = {}
        for edge in batch:
            src_pid = src_pid_map.get(edge["src_id"], "")
            if src_pid:
                edges_by_project.setdefault(src_pid, []).append(edge)

        for src_pid, edges in edges_by_project.items():
            try:
                shard_store = store.shard(src_pid)
                shard_store.add_references_batch(edges)
            except Exception as exc:
                LOGGER.warning("link_dependency_imports: batch write failed for %s: %s", src_pid, exc)

    _ping(f"{new_edges} import-reference edges created")
    LOGGER.info("link_dependency_imports: created %d new reference edges.", new_edges)
    return new_edges
