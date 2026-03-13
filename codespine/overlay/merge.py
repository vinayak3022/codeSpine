from __future__ import annotations

import os
from typing import Any

from codespine.indexer.symbol_builder import file_id

def _load_overlay_docs(overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    if project:
        doc = overlay_store.load_project(project)
        if doc.get("dirty_files") or doc.get("deleted_files"):
            return [doc]
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
            for method in entry.get("methods", []):
                rec = dict(method)
                rec["file_path"] = file_path
                merged.append(rec)
    return merged


def merged_call_edges(store, overlay_store, project: str | None = None) -> list[dict[str, Any]]:
    project_clause = "AND fa.project_id = $proj" if project else ""
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
               coalesce(r.confidence, 0.5) as confidence,
               coalesce(r.reason, 'unknown') as reason
        """,
        params,
    )
    overlay_docs = _load_overlay_docs(overlay_store, project)
    blocked_file_ids = suppressed_file_ids(overlay_docs)
    merged = [
        rec for rec in base
        if rec.get("src_file_id") not in blocked_file_ids and rec.get("dst_file_id") not in blocked_file_ids
    ]
    for doc in overlay_docs:
        for entry in (doc.get("dirty_files") or {}).values():
            src_file_id = entry.get("file_id")
            for edge in entry.get("calls", []):
                rec = dict(edge)
                rec["src_file_id"] = src_file_id
                rec["dst_file_id"] = None
                merged.append(rec)
    return merged
