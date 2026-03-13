from __future__ import annotations

import json
import os
import time
from typing import Any

from codespine.config import SETTINGS
from codespine.indexer.call_resolver import resolve_calls
from codespine.indexer.java_parser import ParsedFile, parse_java_source
from codespine.indexer.symbol_builder import class_id, digest_bytes, file_id, method_id, symbol_id
from codespine.search.vector import embed_text


def _safe_project_filename(project_id: str) -> str:
    return project_id.replace("/", "__").replace(":", "__")


class OverlayStore:
    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = os.path.expanduser(base_dir or SETTINGS.overlay_dir)
        try:
            os.makedirs(self.base_dir, exist_ok=True)
        except OSError:
            self.base_dir = "/tmp/.codespine_overlay"
            os.makedirs(self.base_dir, exist_ok=True)

    def project_path(self, project_id: str) -> str:
        return os.path.join(self.base_dir, f"{_safe_project_filename(project_id)}.json")

    def load_project(self, project_id: str) -> dict[str, Any]:
        path = self.project_path(project_id)
        if not os.path.exists(path):
            return self._empty_doc(project_id)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, TypeError):
            return self._empty_doc(project_id)
        if not isinstance(data, dict):
            return self._empty_doc(project_id)
        doc = self._empty_doc(project_id)
        doc.update(data)
        doc["dirty_files"] = data.get("dirty_files", {}) if isinstance(data.get("dirty_files"), dict) else {}
        deleted = data.get("deleted_files", [])
        doc["deleted_files"] = deleted if isinstance(deleted, list) else []
        return doc

    def save_project(self, project_id: str, data: dict[str, Any]) -> None:
        doc = self._empty_doc(project_id)
        doc.update(data)
        doc["updated_at"] = int(time.time())
        path = self.project_path(project_id)
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(tmp_path, path)

    def list_projects(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(self.base_dir)):
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.base_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(data, dict):
                out.append(data)
        return out

    def clear_project(self, project_id: str) -> None:
        path = self.project_path(project_id)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def clear_all(self) -> None:
        for doc in self.list_projects():
            project_id = doc.get("project_id")
            if isinstance(project_id, str) and project_id:
                self.clear_project(project_id)

    def upsert_file(
        self,
        project_id: str,
        project_path: str,
        repo_root: str | None,
        base_commit: str,
        current_head: str | None,
        file_path: str,
        entry: dict[str, Any],
    ) -> None:
        doc = self.load_project(project_id)
        doc["project_id"] = project_id
        doc["project_path"] = project_path
        doc["repo_root"] = repo_root or project_path
        doc["base_commit"] = base_commit or ""
        doc["current_head"] = current_head or base_commit or ""
        doc["dirty_files"][os.path.abspath(file_path)] = entry
        deleted = [p for p in doc.get("deleted_files", []) if p != os.path.abspath(file_path)]
        doc["deleted_files"] = deleted
        self.save_project(project_id, doc)

    def mark_deleted(
        self,
        project_id: str,
        project_path: str,
        repo_root: str | None,
        base_commit: str,
        current_head: str | None,
        file_path: str,
    ) -> None:
        abs_path = os.path.abspath(file_path)
        doc = self.load_project(project_id)
        doc["project_id"] = project_id
        doc["project_path"] = project_path
        doc["repo_root"] = repo_root or project_path
        doc["base_commit"] = base_commit or ""
        doc["current_head"] = current_head or base_commit or ""
        doc["dirty_files"].pop(abs_path, None)
        deleted = set(doc.get("deleted_files", []))
        deleted.add(abs_path)
        doc["deleted_files"] = sorted(deleted)
        self.save_project(project_id, doc)

    def update_head(self, project_id: str, current_head: str | None) -> None:
        doc = self.load_project(project_id)
        if not doc.get("dirty_files") and not doc.get("deleted_files"):
            return
        doc["current_head"] = current_head or doc.get("current_head") or ""
        self.save_project(project_id, doc)

    def status(self, project_id: str | None = None) -> list[dict[str, Any]]:
        docs = [self.load_project(project_id)] if project_id else self.list_projects()
        out: list[dict[str, Any]] = []
        for doc in docs:
            pid = doc.get("project_id")
            if not pid:
                continue
            dirty_files = doc.get("dirty_files", {})
            deleted_files = doc.get("deleted_files", [])
            out.append(
                {
                    "project_id": pid,
                    "project_path": doc.get("project_path"),
                    "repo_root": doc.get("repo_root"),
                    "base_commit": doc.get("base_commit"),
                    "current_head": doc.get("current_head"),
                    "dirty_file_count": len(dirty_files),
                    "deleted_file_count": len(deleted_files),
                    "overlay_present": bool(dirty_files or deleted_files),
                    "updated_at": doc.get("updated_at"),
                }
            )
        return out

    @staticmethod
    def _empty_doc(project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "project_path": "",
            "repo_root": "",
            "base_commit": "",
            "current_head": "",
            "dirty_files": {},
            "deleted_files": [],
            "updated_at": 0,
        }


def _overlay_method_catalog(doc: dict[str, Any], exclude_file: str | None = None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    exclude_file = os.path.abspath(exclude_file) if exclude_file else None
    for file_path, entry in (doc.get("dirty_files") or {}).items():
        if exclude_file and os.path.abspath(file_path) == exclude_file:
            continue
        for method in entry.get("methods", []):
            out[method["id"]] = {
                "signature": method["signature"],
                "name": method["name"],
                "param_count": int(method.get("param_count", 0)),
                "class_fqcn": method["class_fqcn"],
                "class_id": method["class_id"],
            }
    return out


def _overlay_class_catalog(doc: dict[str, Any], exclude_file: str | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    exclude_file = os.path.abspath(exclude_file) if exclude_file else None
    for file_path, entry in (doc.get("dirty_files") or {}).items():
        if exclude_file and os.path.abspath(file_path) == exclude_file:
            continue
        for cls in entry.get("classes", []):
            out.setdefault(cls["name"], [])
            if cls["fqcn"] not in out[cls["name"]]:
                out[cls["name"]].append(cls["fqcn"])
    return out


def _overlay_class_ids(doc: dict[str, Any], exclude_file: str | None = None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    exclude_file = os.path.abspath(exclude_file) if exclude_file else None
    for file_path, entry in (doc.get("dirty_files") or {}).items():
        if exclude_file and os.path.abspath(file_path) == exclude_file:
            continue
        for cls in entry.get("classes", []):
            out.setdefault(cls["fqcn"], [])
            if cls["id"] not in out[cls["fqcn"]]:
                out[cls["fqcn"]].append(cls["id"])
    return out


def _overlay_class_methods(doc: dict[str, Any], exclude_file: str | None = None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    exclude_file = os.path.abspath(exclude_file) if exclude_file else None
    for file_path, entry in (doc.get("dirty_files") or {}).items():
        if exclude_file and os.path.abspath(file_path) == exclude_file:
            continue
        for method in entry.get("methods", []):
            out.setdefault(method["class_id"], {})
            out[method["class_id"]][method["signature"]] = method["id"]
    return out


def build_overlay_file_entry(
    *,
    store,
    project_id: str,
    project_path: str,
    file_path: str,
    source: bytes,
    embed: bool,
    base_method_catalog: dict[str, dict[str, Any]],
    base_class_catalog: dict[str, list[str]],
    base_class_ids_by_fqcn: dict[str, list[str]],
    base_class_methods: dict[str, dict[str, str]],
    existing_overlay_doc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = parse_java_source(source)
    rel_path = os.path.relpath(file_path, project_path)
    scope = rel_path.replace("\\", "/")
    if "/java/" in scope:
        scope = scope.split("/java/", 1)[0]
    elif "/src/" in scope:
        scope = scope.split("/src/", 1)[0]
    else:
        scope = os.path.dirname(scope).strip() or "."
    f_id = file_id(project_id, rel_path)
    digest = digest_bytes(source)
    is_test = "src/test/java" in file_path.replace("\\", "/")

    overlay_doc = existing_overlay_doc or {}
    method_catalog = dict(base_method_catalog)
    method_catalog.update(_overlay_method_catalog(overlay_doc, exclude_file=file_path))
    class_catalog = {k: list(v) for k, v in base_class_catalog.items()}
    for name, fqcn_list in _overlay_class_catalog(overlay_doc, exclude_file=file_path).items():
        class_catalog.setdefault(name, [])
        for fqcn in fqcn_list:
            if fqcn not in class_catalog[name]:
                class_catalog[name].append(fqcn)
    fqcn_to_class_ids = {k: list(v) for k, v in base_class_ids_by_fqcn.items()}
    for fqcn, class_ids in _overlay_class_ids(overlay_doc, exclude_file=file_path).items():
        fqcn_to_class_ids.setdefault(fqcn, [])
        for cid in class_ids:
            if cid not in fqcn_to_class_ids[fqcn]:
                fqcn_to_class_ids[fqcn].append(cid)
    class_methods = {cid: dict(methods) for cid, methods in base_class_methods.items()}
    for cid, methods in _overlay_class_methods(overlay_doc, exclude_file=file_path).items():
        class_methods.setdefault(cid, {}).update(methods)

    classes: list[dict[str, Any]] = []
    methods: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    method_calls: dict[str, list[Any]] = {}
    method_context: dict[str, dict[str, Any]] = {}
    class_meta: dict[str, dict[str, Any]] = {}

    for cls in parsed.classes:
        c_id = class_id(cls.fqcn, scope)
        class_catalog.setdefault(cls.name, [])
        if cls.fqcn not in class_catalog[cls.name]:
            class_catalog[cls.name].append(cls.fqcn)
        fqcn_to_class_ids.setdefault(cls.fqcn, [])
        if c_id not in fqcn_to_class_ids[cls.fqcn]:
            fqcn_to_class_ids[cls.fqcn].append(c_id)
        class_meta[c_id] = {
            "fqcn": cls.fqcn,
            "package": parsed.package,
            "imports": parsed.imports,
            "extends": cls.extends,
            "interfaces": cls.interfaces,
        }
        class_methods.setdefault(c_id, {})
        classes.append(
            {
                "id": c_id,
                "fqcn": cls.fqcn,
                "name": cls.name,
                "package": cls.package,
                "file_id": f_id,
                "line": cls.line,
                "col": cls.col,
            }
        )
        cls_symbol_id = symbol_id("class", cls.fqcn, scope)
        symbols.append(
            {
                "id": cls_symbol_id,
                "kind": "class",
                "name": cls.name,
                "fqname": cls.fqcn,
                "file_id": f_id,
                "line": cls.line,
                "col": cls.col,
                "file_path": file_path,
                "is_test": is_test,
                "project_id": project_id,
                "embedding": embed_text(f"class {cls.fqcn}") if embed else None,
            }
        )
        for method in cls.methods:
            m_id = method_id(cls.fqcn, method.signature, scope)
            methods.append(
                {
                    "id": m_id,
                    "class_id": c_id,
                    "class_fqcn": cls.fqcn,
                    "name": method.name,
                    "signature": method.signature,
                    "return_type": method.return_type,
                    "modifiers": method.modifiers + [f"@{a}" for a in method.annotations],
                    "is_constructor": bool(method.name == cls.name),
                    "is_test": is_test,
                    "param_count": len(method.parameter_types),
                    "file_id": f_id,
                    "file_path": file_path,
                    "project_id": project_id,
                }
            )
            fqname = f"{cls.fqcn}#{method.signature}"
            symbols.append(
                {
                    "id": symbol_id("method", fqname, scope),
                    "kind": "method",
                    "name": method.name,
                    "fqname": fqname,
                    "file_id": f_id,
                    "line": method.line,
                    "col": method.col,
                    "file_path": file_path,
                    "is_test": is_test,
                    "project_id": project_id,
                    "embedding": embed_text(f"method {fqname} returns {method.return_type}") if embed else None,
                }
            )
            method_catalog[m_id] = {
                "signature": method.signature,
                "name": method.name,
                "param_count": len(method.parameter_types),
                "class_fqcn": cls.fqcn,
                "class_id": c_id,
            }
            method_calls[m_id] = method.calls
            method_context[m_id] = {
                "class_id": c_id,
                "class_fqcn": cls.fqcn,
                "local_types": method.local_types,
                "field_types": cls.field_types,
                "imports": parsed.imports,
                "package": parsed.package,
            }
            class_methods[c_id][method.signature] = m_id

    calls = [
        {"src": src, "dst": dst, "confidence": confidence, "reason": reason}
        for src, dst, confidence, reason in resolve_calls(method_catalog, method_calls, method_context, class_catalog)
    ]

    relations: list[dict[str, Any]] = []
    for src_id, meta in class_meta.items():
        ctx = {"package": meta.get("package", ""), "imports": meta.get("imports", [])}
        extends_name = meta.get("extends")
        candidates: list[str] = []
        if extends_name:
            raw = extends_name.strip()
            simple = raw.split(".")[-1]
            if "." in raw:
                candidates.append(raw)
            for imp in ctx["imports"]:
                if imp.endswith(f".{simple}"):
                    candidates.append(imp)
            if ctx["package"]:
                candidates.append(f"{ctx['package']}.{simple}")
            candidates.extend(class_catalog.get(simple, []))
        seen: set[str] = set()
        for fqcn in candidates:
            if fqcn in seen:
                continue
            seen.add(fqcn)
            for dst_id in fqcn_to_class_ids.get(fqcn, []):
                relations.append(
                    {
                        "rel": "IMPLEMENTS",
                        "src_label": "Class",
                        "src_id": src_id,
                        "dst_label": "Class",
                        "dst_id": dst_id,
                        "confidence": 0.8,
                    }
                )
                for signature, method_id_value in class_methods.get(src_id, {}).items():
                    parent_method = class_methods.get(dst_id, {}).get(signature)
                    if parent_method:
                        relations.append(
                            {
                                "rel": "OVERRIDES",
                                "src_label": "Method",
                                "src_id": method_id_value,
                                "dst_label": "Method",
                                "dst_id": parent_method,
                                "confidence": 1.0,
                            }
                        )
        for iface in meta.get("interfaces", []):
            raw = iface.strip()
            simple = raw.split(".")[-1]
            iface_candidates: list[str] = []
            if "." in raw:
                iface_candidates.append(raw)
            for imp in ctx["imports"]:
                if imp.endswith(f".{simple}"):
                    iface_candidates.append(imp)
            if ctx["package"]:
                iface_candidates.append(f"{ctx['package']}.{simple}")
            iface_candidates.extend(class_catalog.get(simple, []))
            seen = set()
            for fqcn in iface_candidates:
                if fqcn in seen:
                    continue
                seen.add(fqcn)
                for dst_id in fqcn_to_class_ids.get(fqcn, []):
                    relations.append(
                        {
                            "rel": "IMPLEMENTS",
                            "src_label": "Class",
                            "src_id": src_id,
                            "dst_label": "Class",
                            "dst_id": dst_id,
                            "confidence": 1.0,
                        }
                    )
                    for signature, method_id_value in class_methods.get(src_id, {}).items():
                        iface_method = class_methods.get(dst_id, {}).get(signature)
                        if iface_method:
                            relations.append(
                                {
                                    "rel": "OVERRIDES",
                                    "src_label": "Method",
                                    "src_id": method_id_value,
                                    "dst_label": "Method",
                                    "dst_id": iface_method,
                                    "confidence": 1.0,
                                }
                            )

    stat = os.stat(file_path)
    return {
        "file_path": os.path.abspath(file_path),
        "rel_path": rel_path,
        "file_id": f_id,
        "file_hash": digest,
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
        "size": int(stat.st_size),
        "is_test": is_test,
        "project_id": project_id,
        "classes": classes,
        "methods": methods,
        "symbols": symbols,
        "calls": calls,
        "types": relations,
        "parsed": {
            "package": parsed.package,
            "imports": parsed.imports,
            "class_count": len(parsed.classes),
        },
    }
