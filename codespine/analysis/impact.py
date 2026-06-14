from __future__ import annotations

from collections import defaultdict, deque

from codespine.overlay.merge import merged_call_edges, merged_method_records, merged_symbol_records


def _symbol_exact_match(rec: dict, needle: str) -> bool:
    return (
        str(rec.get("id") or "").lower() == needle
        or str(rec.get("name") or "").lower() == needle
        or str(rec.get("fqname") or "").lower() == needle
    )


def _resolve_exact_symbol_records(store, symbol_query: str, project: str | None = None) -> list[dict]:
    overlay_store = getattr(store, "overlay_store", None)
    needle = (symbol_query or "").strip().lower()
    if not needle:
        return []
    if overlay_store is not None:
        recs = [rec for rec in merged_symbol_records(store, overlay_store, project=project) if _symbol_exact_match(rec, needle)]
    else:
        project_clause = "AND f.project_id = $proj" if project else ""
        params: dict = {"q": symbol_query}
        if project:
            params["proj"] = project
        recs = store.query_records(
            f"""
            MATCH (s:Symbol), (f:File)
            WHERE s.file_id = f.id {project_clause}
            AND (s.id = $q OR lower(s.name) = lower($q) OR lower(s.fqname) = lower($q))
            RETURN s.id as id, s.kind as kind, s.name as name, s.fqname as fqname,
                   s.file_id as file_id, f.project_id as project_id, f.path as file_path,
                   f.is_test as is_test
            """,
            params,
        )
    seen: set[str] = set()
    out: list[dict] = []
    for rec in recs:
        rec_id = rec.get("id")
        if not rec_id or rec_id in seen:
            continue
        seen.add(str(rec_id))
        out.append(rec)
    return out


def _resolve_methods_for_symbol(store, symbol_rec: dict, project: str | None = None) -> list[str]:
    overlay_store = getattr(store, "overlay_store", None)
    symbol_kind = str(symbol_rec.get("kind") or "").lower()
    fqname = str(symbol_rec.get("fqname") or "")
    symbol_name = str(symbol_rec.get("name") or "")
    method_ids: list[str] = []

    def _append_method_id(mid: str | None) -> None:
        mid = str(mid or "")
        if mid and mid not in method_ids:
            method_ids.append(mid)

    def _append_test_companions() -> None:
        if not project or symbol_kind != "method" or not symbol_name or symbol_name.lower().startswith("test"):
            return
        candidates = [f"test{symbol_name[0].upper()}{symbol_name[1:]}", f"test_{symbol_name}"]
        if overlay_store is not None:
            for rec in merged_method_records(store, overlay_store, project=project):
                rec_name = str(rec.get("name") or "")
                rec_sig = str(rec.get("signature") or "")
                if any(rec_name.lower() == candidate.lower() or rec_sig.lower() == f"{candidate}()".lower() for candidate in candidates):
                    _append_method_id(rec.get("id"))
            return
        if not hasattr(store, "query_records"):
            return
        project_clause = "AND f.project_id = $proj" if project else ""
        for candidate in candidates:
            params = {"q": candidate}
            if project:
                params["proj"] = project
            rows = store.query_records(
                f"""
                MATCH (m:Method), (c:Class), (f:File)
                WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
                  AND (lower(m.name) = lower($q) OR lower(m.signature) = lower($q))
                RETURN m.id as id, m.name as name, m.signature as fqname,
                       c.fqcn as class_fqcn, f.project_id as project_id, f.path as file_path
                """,
                params,
            )
            for row in rows:
                _append_method_id(row.get("id"))

    if symbol_kind == "class":
        class_fqcn = fqname or str(symbol_rec.get("name") or "")
        if not class_fqcn:
            return []
        if overlay_store is not None:
            method_ids = []
            for rec in merged_method_records(store, overlay_store, project=project):
                if str(rec.get("class_fqcn") or "").lower() == class_fqcn.lower():
                    method_ids.append(str(rec["id"]))
            return method_ids
        project_clause = "AND f.project_id = $proj" if project else ""
        params: dict = {"class_fqcn": class_fqcn}
        if project:
            params["proj"] = project
        rows = store.query_records(
            f"""
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
              AND lower(c.fqcn) = lower($class_fqcn)
            RETURN m.id as id
                """,
                params,
            )
        for row in rows:
            _append_method_id(row.get("id"))
        return method_ids

    if fqname and "#" in fqname:
        class_fqcn, signature = fqname.rsplit("#", 1)
        if overlay_store is not None:
            for rec in merged_method_records(store, overlay_store, project=project):
                if str(rec.get("class_fqcn") or "").lower() == class_fqcn.lower() and str(rec.get("signature") or "").lower() == signature.lower():
                    _append_method_id(rec.get("id"))
        elif hasattr(store, "query_records"):
            project_clause = "AND f.project_id = $proj" if project else ""
            params = {"class_fqcn": class_fqcn, "signature": signature}
            if project:
                params["proj"] = project
            rows = store.query_records(
                f"""
                MATCH (m:Method), (c:Class), (f:File)
                WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
                  AND lower(c.fqcn) = lower($class_fqcn)
                  AND lower(m.signature) = lower($signature)
                RETURN m.id as id, m.name as name, m.signature as fqname,
                       c.fqcn as class_fqcn, f.project_id as project_id, f.path as file_path
                """,
                params,
            )
            for row in rows:
                _append_method_id(row.get("id"))

    _append_test_companions()

    if method_ids:
        return method_ids

    if not symbol_rec.get("id"):
        return []

    # Compatibility fallback for older test/store query shapes that only expose
    # a symbol-to-method join. Keep it project-scoped and exact on the symbol id.
    if not hasattr(store, "query_records"):
        return []
    params = {"sid": symbol_rec["id"]}
    project_clause = "AND f.project_id = $proj" if project else ""
    if project:
        params["proj"] = project
    rows = store.query_records(
        f"""
        MATCH (s:Symbol), (m:Method), (c:Class), (f:File)
        WHERE s.file_id = f.id AND m.class_id = c.id AND c.file_id = f.id {project_clause}
          AND s.id = $sid
        RETURN s.id as sid, m.id as mid
        """,
        params,
    )
    return [str(row["mid"]) for row in rows if row.get("mid")]


def resolve_symbol_targets(store, symbol_query: str, project: str | None = None) -> dict:
    exact_matches = _resolve_exact_symbol_records(store, symbol_query, project=project)
    if not exact_matches:
        return {"status": "not_found", "matches": [], "resolved_method_ids": []}
    if len(exact_matches) > 1:
        # Ambiguity resolution: if one match is clearly better (e.g. a non-test
        # class when all others are test or different kinds), resolve to that.
        # Otherwise return ambiguous so callers can decide.
        best = _pick_best_symbol(exact_matches)
        if best is not None:
            method_ids = _resolve_methods_for_symbol(store, best, project=project)
            return {"status": "exact", "matches": [best], "resolved_method_ids": method_ids}
        return {"status": "ambiguous", "matches": exact_matches, "resolved_method_ids": []}

    symbol_rec = exact_matches[0]
    method_ids = _resolve_methods_for_symbol(store, symbol_rec, project=project)
    return {"status": "exact", "matches": exact_matches, "resolved_method_ids": method_ids}


def _pick_best_symbol(matches: list[dict]) -> dict | None:
    """From a list of ambiguous symbol matches, pick the best one.

    Priority:
      1. Non-test class symbols (clearly the intended target)
      2. Any non-test symbol when all others are test symbols
      3. None — if no clear winner exists

    Returns None when all candidates are equally ambiguous (e.g. two methods
    with the same name in different classes, or multiple test classes).
    """
    if not matches:
        return None

    non_test = [m for m in matches if not m.get("is_test")]
    non_test_classes = [m for m in non_test if str(m.get("kind") or "").lower() == "class"]

    # Prefer a non-test class match above all else
    if len(non_test_classes) == 1:
        return non_test_classes[0]

    # If we have multiple non-test classes, pick by name (prefer the shortest
    # FQCN, which is typically the "main" class vs a test or companion)
    if len(non_test_classes) > 1:
        non_test_classes.sort(key=lambda m: len(str(m.get("fqname") or m.get("name") or "")))
        return non_test_classes[0]

    # If there's exactly one non-test symbol among all matches, pick it
    if len(non_test) == 1:
        return non_test[0]

    # All non-test matches (if any) are equally ambiguous — give up
    return None


def _resolve_method_metadata(store, method_ids: list[str], project: str | None = None) -> dict[str, dict]:
    """Bulk-resolve method IDs to human-readable metadata in a single query.

    Returns a dict keyed by method ID with fields:
      name, fqname (= m.signature), class_fqcn, file_path, project_id.
    Any ID not found in the graph is silently omitted.
    """
    if not method_ids:
        return {}
    overlay_store = getattr(store, "overlay_store", None)
    if overlay_store is not None:
        recs = [r for r in merged_method_records(store, overlay_store, project=project) if r.get("id") in set(method_ids)]
        for rec in recs:
            rec["fqname"] = rec.get("signature")
    else:
        project_clause = "AND f.project_id = $proj" if project else ""
        params: dict = {"ids": method_ids}
        if project:
            params["proj"] = project
        recs = store.query_records(
            f"""
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.id IN $ids AND m.class_id = c.id AND c.file_id = f.id {project_clause}
            RETURN m.id as id, m.name as name, m.signature as fqname,
                   c.fqcn as class_fqcn, f.path as file_path, f.project_id as project_id
            """,
            params,
        )
    return {r["id"]: r for r in recs}


def analyze_impact(store, symbol_query: str, max_depth: int = 4, project: str | None = None) -> dict:
    resolution = resolve_symbol_targets(store, symbol_query, project=project)
    if resolution["status"] != "exact":
        payload = {
            "target": symbol_query,
            "resolution": resolution,
            "depth_groups": {"1": [], "2": [], "3+": []},
        }
        if resolution["status"] == "ambiguous":
            payload["ambiguity"] = {"matches": resolution["matches"]}
        return payload

    overlay_store = getattr(store, "overlay_store", None)
    target_method_ids = resolution["resolved_method_ids"]
    if not target_method_ids:
        return {"target": symbol_query, "resolution": resolution, "depth_groups": {"1": [], "2": [], "3+": []}}

    # Load call edges; when project is provided, keep traversal within that scope.
    if overlay_store is not None:
        edges = merged_call_edges(store, overlay_store, project=project)
        for edge in edges:
            edge["edge_type"] = "CALLS"
    else:
        if project:
            edges = store.query_records(
                """
                MATCH (a:Method)-[r:CALLS]->(b:Method), (ca:Class), (fa:File), (cb:Class), (fb:File)
                WHERE a.class_id = ca.id AND ca.file_id = fa.id
                  AND b.class_id = cb.id AND cb.file_id = fb.id
                  AND fa.project_id = $proj AND fb.project_id = $proj
                RETURN a.id as src, b.id as dst, 'CALLS' as edge_type,
                       coalesce(r.confidence, 0.5) as confidence,
                       coalesce(r.reason, 'unknown') as reason
                """,
                {"proj": project},
            )
        else:
            edges = store.query_records(
                """
                MATCH (a:Method)-[r:CALLS]->(b:Method)
                RETURN a.id as src, b.id as dst, 'CALLS' as edge_type,
                       coalesce(r.confidence, 0.5) as confidence,
                       coalesce(r.reason, 'unknown') as reason
                """
            )

    # Augment with DI injection edges: for each target method's class, find all
    # classes that @Inject it (or bind it via @Component/@Service) and add their
    # methods as implicit callers at depth+1 with edge_type "DI_INJECT".
    try:
        if project:
            di_edges = store.query_records(
                """
                MATCH (a:Method), (ca:Class)-[r:INJECTS]->(cb:Class), (b:Method), (fa:File), (fb:File)
                WHERE a.class_id = ca.id AND ca.file_id = fa.id
                  AND b.class_id = cb.id AND cb.file_id = fb.id
                  AND fa.project_id = $proj AND fb.project_id = $proj
                RETURN a.id as src, b.id as dst, 'DI_INJECT' as edge_type,
                       coalesce(r.confidence, 0.8) as confidence,
                       coalesce(r.binding_type, 'field_inject') as reason
                """,
                {"proj": project},
            )
        else:
            di_edges = store.query_records(
                """
                MATCH (a:Method), (ca:Class), (b:Method), (cb:Class),
                      (ca)-[r:INJECTS]->(cb)
                WHERE a.class_id = ca.id AND b.class_id = cb.id
                RETURN a.id as src, b.id as dst, 'DI_INJECT' as edge_type,
                       coalesce(r.confidence, 0.8) as confidence,
                       coalesce(r.binding_type, 'field_inject') as reason
                """
            )
        edges = list(edges) + di_edges
    except Exception:
        pass  # INJECTS table may not exist on old DBs

    # Also follow BINDS_INTERFACE — any class implementing the target's interface
    # counts as an indirect caller.
    try:
        if project:
            bi_edges = store.query_records(
                """
                MATCH (a:Method), (ca:Class)-[r:BINDS_INTERFACE]->(cb:Class), (b:Method), (fa:File), (fb:File)
                WHERE a.class_id = ca.id AND ca.file_id = fa.id
                  AND b.class_id = cb.id AND cb.file_id = fb.id
                  AND fa.project_id = $proj AND fb.project_id = $proj
                RETURN a.id as src, b.id as dst, 'INTERFACE_BINDING' as edge_type,
                       coalesce(r.confidence, 0.9) as confidence,
                       coalesce(r.reason, 'implements') as reason
                """,
                {"proj": project},
            )
        else:
            bi_edges = store.query_records(
                """
                MATCH (a:Method), (ca:Class), (b:Method), (cb:Class),
                      (ca)-[r:BINDS_INTERFACE]->(cb)
                WHERE a.class_id = ca.id AND b.class_id = cb.id
                RETURN a.id as src, b.id as dst, 'INTERFACE_BINDING' as edge_type,
                       coalesce(r.confidence, 0.9) as confidence,
                       coalesce(r.reason, 'implements') as reason
                """
            )
        edges = list(edges) + bi_edges
    except Exception:
        pass

    reverse_adj: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        reverse_adj[edge["dst"]].append(edge)

    depth_groups: dict[str, list[dict]] = {"1": [], "2": [], "3+": []}
    visited: set[str] = set(target_method_ids)
    queue = deque([(mid, 0, [mid]) for mid in target_method_ids])

    while queue:
        node, depth, path = queue.popleft()
        if depth >= max_depth:
            continue
        for edge in reverse_adj.get(node, []):
            src = edge["src"]
            if src in visited:
                continue
            visited.add(src)
            next_depth = depth + 1
            item = {
                "symbol": src,
                "depth": next_depth,
                "edge_type": edge["edge_type"],
                "confidence": float(edge["confidence"]),
                "path": path + [src],
            }
            if next_depth == 1:
                depth_groups["1"].append(item)
            elif next_depth == 2:
                depth_groups["2"].append(item)
            else:
                depth_groups["3+"].append(item)
            queue.append((src, next_depth, path + [src]))

    # ------------------------------------------------------------------ #
    # Enrich every caller entry with human-readable metadata so AI agents
    # don't need a second round-trip to resolve raw ID hashes.
    # A single bulk query resolves all collected method IDs at once.
    # ------------------------------------------------------------------ #
    all_caller_ids = [item["symbol"] for items in depth_groups.values() for item in items]
    meta = _resolve_method_metadata(store, all_caller_ids, project=project)

    for items in depth_groups.values():
        for item in items:
            m = meta.get(item["symbol"], {})
            item["name"] = m.get("name")
            item["fqname"] = m.get("fqname")
            item["file_path"] = m.get("file_path")
            item["project_id"] = m.get("project_id")
            item["class_fqcn"] = m.get("class_fqcn")
            # Convert the call-path from a list of raw IDs to human-readable names
            # so an agent can read the chain without additional lookups.
            item["path"] = [
                meta.get(pid, {}).get("name") or pid
                for pid in item["path"]
            ]

    # Also enrich the targets_resolved list for context
    target_meta = _resolve_method_metadata(store, target_method_ids, project=project)
    resolved_targets = [
        {
            "id": mid,
            "name": target_meta.get(mid, {}).get("name"),
            "fqname": target_meta.get(mid, {}).get("fqname"),
            "file_path": target_meta.get(mid, {}).get("file_path"),
            "class_fqcn": target_meta.get(mid, {}).get("class_fqcn"),
            "project_id": target_meta.get(mid, {}).get("project_id"),
        }
        for mid in target_method_ids
    ]

    # FR-06: Separate "self_callers" (same class as target, depth=1) from
    # impacted_callers so the output is unambiguous.
    target_class_fqcns = {
        target_meta.get(mid, {}).get("class_fqcn")
        for mid in target_method_ids
        if target_meta.get(mid, {}).get("class_fqcn")
    }
    self_callers: list[dict] = []
    impacted_depth1: list[dict] = []
    for item in depth_groups["1"]:
        if item.get("class_fqcn") and item["class_fqcn"] in target_class_fqcns:
            self_callers.append(item)
        else:
            impacted_depth1.append(item)
    depth_groups["1"] = impacted_depth1

    return {
        "target": symbol_query,
        "resolution": resolution,
        "resolved_to": resolved_targets,
        "self_callers": self_callers,
        "impacted_callers": depth_groups,
        "summary": {
            "direct": len(depth_groups["1"]),
            "indirect": len(depth_groups["2"]),
            "transitive": len(depth_groups["3+"]),
            "self_callers": len(self_callers),
        },
    }
