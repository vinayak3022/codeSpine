from __future__ import annotations

import os
from typing import Any

from codespine.indexer.symbol_builder import file_id


def _within_project_path(doc: dict[str, Any], file_path: str | None) -> bool:
    project_path = os.path.abspath(str(doc.get("project_path") or ""))
    if not project_path or not file_path:
        return True
    try:
        return os.path.commonpath([os.path.abspath(file_path), project_path]) == project_path
    except ValueError:
        return False

def _load_overlay_docs(overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    if overlay_store is None:
        return []
    if project:
        if not hasattr(overlay_store, "load_project"):
            return []
        doc = overlay_store.load_project(project)
        if doc.get("dirty_files") or doc.get("deleted_files"):
            return [doc]
        return []
    if not hasattr(overlay_store, "list_projects"):
        return []
    docs = []
    for doc in overlay_store.list_projects():
        if doc.get("dirty_files") or doc.get("deleted_files"):
            docs.append(doc)
    return docs


def suppressed_file_ids(overlay_docs: list[dict[str, Any]]) -> set[str]:
    blocked: set[str] = set()
    for doc in overlay_docs:
        for entry in (doc.get("dirty_files") or {}).values():
            file_id = entry.get("file_id")
            if file_id:
                blocked.add(file_id)
        for deleted in doc.get("deleted_files", []):
            blocked.add(_deleted_file_id(doc, deleted))
    return blocked


def _deleted_file_id(doc: dict[str, Any], file_path: str) -> str:
    project_id = str(doc.get("project_id") or "")
    project_path = str(doc.get("project_path") or "")
    if not project_id or not project_path:
        return ""
    try:
        rel_path = os.path.relpath(file_path, project_path)
    except ValueError:
        return ""
    return file_id(project_id, rel_path)


def overlay_summary(overlay_store, project: str | None = None) -> dict[str, Any]:
    docs = _load_overlay_docs(overlay_store, project)
    dirty_projects = [doc["project_id"] for doc in docs if doc.get("project_id")]
    dirty_files = sum(len(doc.get("dirty_files", {})) for doc in docs)
    deleted_files = sum(len(doc.get("deleted_files", [])) for doc in docs)
    return {
        "overlay_enabled": True,
        "overlay_mode": "merged",
        "deep_analysis_scope": "base_only",
        "dirty_projects": dirty_projects,
        "dirty_file_count": dirty_files,
        "deleted_file_count": deleted_files,
        "overlay_present": bool(dirty_projects),
    }


def merged_symbol_records(store, overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    project_clause = "AND f.project_id = $proj" if project else ""
    params: dict[str, Any] = {"proj": project} if project else {}
    base = store.query_records(
        f"""
        MATCH (s:Symbol), (f:File)
        WHERE s.file_id = f.id {project_clause}
        RETURN s.id as id,
               s.kind as kind,
               s.name as name,
               s.fqname as fqname,
               s.embedding as embedding,
               s.line as line,
               s.col as col,
               s.file_id as file_id,
               f.path as file_path,
               f.project_id as project_id,
               f.is_test as is_test
        """,
        params,
    )
    overlay_docs = _load_overlay_docs(overlay_store, project)
    blocked_file_ids = suppressed_file_ids(overlay_docs)
    merged = [rec for rec in base if rec.get("file_id") not in blocked_file_ids]
    for doc in overlay_docs:
        for file_path, entry in (doc.get("dirty_files") or {}).items():
            if not _within_project_path(doc, file_path):
                continue
            for symbol in entry.get("symbols", []):
                rec = dict(symbol)
                rec["file_path"] = file_path
                merged.append(rec)
    return merged


def merged_class_records(store, overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    project_clause = "AND f.project_id = $proj" if project else ""
    params: dict[str, Any] = {"proj": project} if project else {}
    base = store.query_records(
        f"""
        MATCH (c:Class), (f:File)
        WHERE c.file_id = f.id {project_clause}
        RETURN c.id as id,
               c.name as name,
               c.fqcn as fqcn,
               c.package as package,
               c.file_id as file_id,
               f.project_id as project_id,
               f.path as file_path
        """,
        params,
    )
    overlay_docs = _load_overlay_docs(overlay_store, project)
    blocked_file_ids = suppressed_file_ids(overlay_docs)
    merged = [rec for rec in base if rec.get("file_id") not in blocked_file_ids]
    for doc in overlay_docs:
        for file_path, entry in (doc.get("dirty_files") or {}).items():
            if not _within_project_path(doc, file_path):
                continue
            for cls in entry.get("classes", []):
                rec = dict(cls)
                rec["project_id"] = doc.get("project_id")
                rec["file_path"] = file_path
                merged.append(rec)
    return merged


def merged_method_records(store, overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    project_clause = "AND f.project_id = $proj" if project else ""
    params: dict[str, Any] = {"proj": project} if project else {}
    base = store.query_records(
        f"""
        MATCH (m:Method), (c:Class), (f:File)
        WHERE m.class_id = c.id AND c.file_id = f.id {project_clause}
        RETURN m.id as id,
               m.class_id as class_id,
               c.fqcn as class_fqcn,
               m.name as name,
               m.signature as signature,
               m.return_type as return_type,
               m.is_constructor as is_constructor,
               m.is_test as is_test,
               c.file_id as file_id,
               f.project_id as project_id,
               f.path as file_path
        """,
        params,
    )
    overlay_docs = _load_overlay_docs(overlay_store, project)
    blocked_file_ids = suppressed_file_ids(overlay_docs)
    merged = [rec for rec in base if rec.get("file_id") not in blocked_file_ids]
    for doc in overlay_docs:
        for file_path, entry in (doc.get("dirty_files") or {}).items():
            if not _within_project_path(doc, file_path):
                continue
            for method in entry.get("methods", []):
                rec = dict(method)
                rec["project_id"] = doc.get("project_id")
                rec["file_path"] = file_path
                merged.append(rec)
    return merged


def _edge_metadata(method: dict[str, Any] | None, prefix: str) -> dict[str, Any]:
    if not method:
        return {}
    return {
        f"{prefix}_file_id": method.get("file_id"),
        f"{prefix}_file_path": method.get("file_path"),
        f"{prefix}_project_id": method.get("project_id"),
    }


def merged_call_edges(store, overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    project_clause = "AND fa.project_id = $proj AND fb.project_id = $proj" if project else ""
    params: dict[str, Any] = {"proj": project} if project else {}
    base = store.query_records(
        f"""
        MATCH (a:Method)-[r:CALLS]->(b:Method), (ca:Class), (fa:File), (cb:Class), (fb:File)
        WHERE a.class_id = ca.id AND ca.file_id = fa.id
          AND b.class_id = cb.id AND cb.file_id = fb.id
          {project_clause}
        RETURN a.id as src,
               b.id as dst,
               ca.file_id as src_file_id,
               cb.file_id as dst_file_id,
               fa.path as src_file_path,
               fb.path as dst_file_path,
               fa.project_id as src_project_id,
               fb.project_id as dst_project_id,
               coalesce(r.confidence, 0.5) as confidence,
               coalesce(r.reason, 'unknown') as reason
        """,
        params,
    )
    overlay_docs = _load_overlay_docs(overlay_store, project)
    method_records = {rec["id"]: rec for rec in merged_method_records(store, overlay_store, project=project) if rec.get("id")}
    merged = []
    for rec in base:
        src_meta = method_records.get(rec.get("src"))
        dst_meta = method_records.get(rec.get("dst"))
        if not src_meta or not dst_meta:
            continue
        merged.append({**rec, **_edge_metadata(src_meta, "src"), **_edge_metadata(dst_meta, "dst"), "edge_type": "CALLS"})
    for doc in overlay_docs:
        for file_path, entry in (doc.get("dirty_files") or {}).items():
            if not _within_project_path(doc, file_path):
                continue
            src_file_id = entry.get("file_id")
            for edge in entry.get("calls", []):
                src_meta = method_records.get(edge.get("src"))
                dst_meta = method_records.get(edge.get("dst"))
                if not src_meta or not dst_meta:
                    continue
                rec = dict(edge)
                rec.update(
                    {
                        "src_file_id": src_meta.get("file_id") or src_file_id,
                        "dst_file_id": dst_meta.get("file_id"),
                        "edge_type": "CALLS",
                        **_edge_metadata(src_meta, "src"),
                        **_edge_metadata(dst_meta, "dst"),
                    }
                )
                merged.append(rec)
    return merged


def merged_reference_edges(store, overlay_store, project: str | None = None, rel: str = "REFERENCES_TYPE") -> list[dict[str, Any]]:
    """Load symbol-level type-reference edges with project/kind metadata.

    Queries the base store for ``REFERENCES_TYPE`` edges (or *rel*) and
    merges with overlay dirty-file reference edges when an overlay store
    is available.  Returns a list of dicts with keys::

        src, dst, src_name, dst_name, src_fqname, dst_fqname,
        src_file_path, dst_file_path, src_project_id, dst_project_id,
        confidence, rel
    """
    from codespine.project_state import project_dependency_closure as _pd_closure

    scope_projects: set[str] | None = None
    if project:
        scope_projects = set(_pd_closure(project, include_self=True))

    params: dict = {}
    extra: str = ""
    if project and scope_projects and len(scope_projects) == 1:
        extra = "AND fa.project_id = $proj AND fb.project_id = $proj"
        params["proj"] = project

    base = store.query_records(
        f"""
        MATCH (src:Symbol)-[r:{rel}]->(dst:Symbol), (fa:File), (fb:File)
        WHERE src.file_id = fa.id AND dst.file_id = fb.id
        {extra}
        RETURN src.id as src, dst.id as dst,
               src.name as src_name, dst.name as dst_name,
               src.fqname as src_fqname, dst.fqname as dst_fqname,
               fa.path as src_file_path, fb.path as dst_file_path,
               fa.project_id as src_project_id, fb.project_id as dst_project_id,
               coalesce(r.confidence, 0.5) as confidence
        """,
        params,
    )

    if scope_projects and not (len(scope_projects) == 1 and project):
        base = [
            r for r in base
            if r.get("src_project_id") in scope_projects or r.get("dst_project_id") in scope_projects
        ]

    overlay_docs = _load_overlay_docs(overlay_store, project)
    for doc in overlay_docs:
        for file_path, entry in (doc.get("dirty_files") or {}).items():
            for ref in entry.get("references", []):
                base.append({
                    "src": ref.get("src"),
                    "dst": ref.get("dst"),
                    "src_name": ref.get("src_name"),
                    "dst_name": ref.get("dst_name"),
                    "src_fqname": ref.get("src_fqname"),
                    "dst_fqname": ref.get("dst_fqname"),
                    "src_file_path": file_path,
                    "dst_file_path": ref.get("dst_file_path"),
                    "src_project_id": doc.get("project_id"),
                    "dst_project_id": ref.get("dst_project_id", doc.get("project_id")),
                    "confidence": float(ref.get("confidence", 0.9)),
                })

    return base
