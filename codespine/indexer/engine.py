from __future__ import annotations

import os
from dataclasses import dataclass

from codespine.indexer.call_resolver import resolve_calls
from codespine.indexer.java_parser import parse_java_source
from codespine.indexer.symbol_builder import class_id, digest_bytes, file_id, method_id, symbol_id
from codespine.search.vector import embed_text


@dataclass
class IndexResult:
    project_id: str
    files_indexed: int
    classes_indexed: int
    methods_indexed: int


class JavaIndexer:
    def __init__(self, store):
        self.store = store

    def index_project(self, root_path: str, full: bool = True) -> IndexResult:
        root_path = os.path.abspath(root_path)
        project_id = os.path.basename(root_path)
        current_files = self._collect_java_files(root_path)
        current_hashes = self._hash_files(project_id, root_path, current_files)
        db_files = self.store.project_file_hashes(project_id) if not full else {}

        if full:
            to_reindex = current_files
            deleted_file_ids = []
        else:
            to_reindex = []
            deleted_file_ids = [fid for fid in db_files if fid not in current_hashes]
            for file_path in current_files:
                rel_path = os.path.relpath(file_path, root_path)
                fid = file_id(project_id, rel_path)
                digest = current_hashes[fid]
                old = db_files.get(fid, {}).get("hash")
                if old != digest:
                    to_reindex.append(file_path)

        files_indexed = 0
        classes_indexed = 0
        methods_indexed = 0

        method_catalog: dict[str, dict] = self._existing_method_catalog(project_id) if not full else {}
        method_calls: dict[str, list] = {}
        method_context: dict[str, dict] = {}
        class_catalog: dict[str, list[str]] = self._existing_class_catalog(project_id) if not full else {}
        class_meta: dict[str, dict] = {}
        class_methods: dict[str, dict[str, str]] = self._existing_class_methods(project_id) if not full else {}

        with self.store.transaction():
            self.store.upsert_project(project_id, root_path)
            if full:
                self.store.clear_project(project_id)
            else:
                for fid in deleted_file_ids:
                    self.store.clear_file(fid)

            for file_path in to_reindex:
                rel_path = os.path.relpath(file_path, root_path)
                is_test = "src/test/java" in file_path.replace("\\", "/")

                with open(file_path, "rb") as f:
                    source = f.read()

                parsed = parse_java_source(source)
                f_id = file_id(project_id, rel_path)
                if not full:
                    # Drop old symbols/methods/classes for changed files before reinserting.
                    self.store.clear_file(f_id)
                self.store.upsert_file(f_id, file_path, project_id, is_test, digest_bytes(source))

                for cls in parsed.classes:
                    c_id = class_id(cls.fqcn)
                    self.store.upsert_class(c_id, cls.fqcn, cls.name, cls.package, f_id)
                    class_catalog.setdefault(cls.name, [])
                    if cls.fqcn not in class_catalog[cls.name]:
                        class_catalog[cls.name].append(cls.fqcn)
                    class_meta[cls.fqcn] = {
                        "package": parsed.package,
                        "imports": parsed.imports,
                        "extends": cls.extends,
                        "interfaces": cls.interfaces,
                    }
                    class_methods.setdefault(cls.fqcn, {})

                    cls_symbol_id = symbol_id("class", cls.fqcn)
                    self.store.upsert_symbol(
                        symbol_id=cls_symbol_id,
                        kind="class",
                        name=cls.name,
                        fqname=cls.fqcn,
                        file_id=f_id,
                        line=cls.line,
                        col=cls.col,
                        embedding=embed_text(f"class {cls.fqcn}"),
                    )
                    classes_indexed += 1

                    for method in cls.methods:
                        m_id = method_id(cls.fqcn, method.signature)
                        self.store.upsert_method(
                            method_id=m_id,
                            class_id=c_id,
                            name=method.name,
                            signature=method.signature,
                            return_type=method.return_type,
                            modifiers=method.modifiers + [f"@{a}" for a in method.annotations],
                            is_constructor=(method.name == cls.name),
                            is_test=is_test,
                        )

                        fqname = f"{cls.fqcn}#{method.signature}"
                        m_symbol_id = symbol_id("method", fqname)
                        self.store.upsert_symbol(
                            symbol_id=m_symbol_id,
                            kind="method",
                            name=method.name,
                            fqname=fqname,
                            file_id=f_id,
                            line=method.line,
                            col=method.col,
                            embedding=embed_text(f"method {fqname} returns {method.return_type}"),
                        )
                        methods_indexed += 1

                        method_catalog[m_id] = {
                            "signature": method.signature,
                            "name": method.name,
                            "param_count": len(method.parameter_types),
                            "class_fqcn": cls.fqcn,
                        }
                        method_calls[m_id] = method.calls
                        method_context[m_id] = {
                            "class_fqcn": cls.fqcn,
                            "local_types": method.local_types,
                            "field_types": cls.field_types,
                            "imports": parsed.imports,
                            "package": parsed.package,
                        }
                        class_methods[cls.fqcn][method.signature] = m_id
                files_indexed += 1

            for src, dst, confidence, reason in resolve_calls(method_catalog, method_calls, method_context, class_catalog):
                self.store.add_call(src, dst, confidence, reason)

            self._build_inheritance_edges(class_meta, class_catalog, class_methods)

        return IndexResult(
            project_id=project_id,
            files_indexed=files_indexed,
            classes_indexed=classes_indexed,
            methods_indexed=methods_indexed,
        )

    @staticmethod
    def _collect_java_files(root_path: str) -> list[str]:
        out: list[str] = []
        for root, _, files in os.walk(root_path):
            if "src" not in root:
                continue
            if any(skip in root for skip in ["target", "build", "out", ".git"]):
                continue
            for filename in files:
                if filename.endswith(".java"):
                    out.append(os.path.join(root, filename))
        return out

    @staticmethod
    def _hash_files(project_id: str, root_path: str, files: list[str]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for fp in files:
            rel = os.path.relpath(fp, root_path)
            fid = file_id(project_id, rel)
            with open(fp, "rb") as f:
                hashes[fid] = digest_bytes(f.read())
        return hashes

    def _existing_method_catalog(self, project_id: str) -> dict[str, dict]:
        recs = self.store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            RETURN m.id as method_id, m.name as name, m.signature as signature, c.fqcn as class_fqcn
            """,
            {"pid": project_id},
        )
        out: dict[str, dict] = {}
        for r in recs:
            sig = r.get("signature") or ""
            arg_str = sig[sig.find("(") + 1 : sig.rfind(")")] if "(" in sig and ")" in sig else ""
            param_count = 0 if not arg_str else arg_str.count(",") + 1
            out[r["method_id"]] = {
                "signature": sig,
                "name": r.get("name", ""),
                "param_count": param_count,
                "class_fqcn": r.get("class_fqcn", ""),
            }
        return out

    def _existing_class_catalog(self, project_id: str) -> dict[str, list[str]]:
        recs = self.store.query_records(
            """
            MATCH (c:Class), (f:File)
            WHERE c.file_id = f.id AND f.project_id = $pid
            RETURN c.name as name, c.fqcn as fqcn
            """,
            {"pid": project_id},
        )
        out: dict[str, list[str]] = {}
        for r in recs:
            out.setdefault(r["name"], [])
            if r["fqcn"] not in out[r["name"]]:
                out[r["name"]].append(r["fqcn"])
        return out

    def _existing_class_methods(self, project_id: str) -> dict[str, dict[str, str]]:
        recs = self.store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            RETURN c.fqcn as fqcn, m.signature as signature, m.id as method_id
            """,
            {"pid": project_id},
        )
        out: dict[str, dict[str, str]] = {}
        for r in recs:
            out.setdefault(r["fqcn"], {})
            out[r["fqcn"]][r["signature"]] = r["method_id"]
        return out

    @staticmethod
    def _resolve_type_candidates(type_name: str | None, context: dict, class_catalog: dict[str, list[str]]) -> list[str]:
        if not type_name:
            return []
        raw = type_name.strip()
        simple = raw.split(".")[-1]
        candidates: list[str] = []
        if "." in raw:
            candidates.append(raw)
        for imp in context.get("imports", []) or []:
            if imp.endswith(f".{simple}"):
                candidates.append(imp)
        pkg = context.get("package", "")
        if pkg:
            candidates.append(f"{pkg}.{simple}")
        candidates.extend(class_catalog.get(simple, []))
        uniq: list[str] = []
        seen = set()
        for c in candidates:
            if c and c not in seen:
                uniq.append(c)
                seen.add(c)
        return uniq

    def _build_inheritance_edges(
        self,
        class_meta: dict[str, dict],
        class_catalog: dict[str, list[str]],
        class_methods: dict[str, dict[str, str]],
    ) -> None:
        for fqcn, meta in class_meta.items():
            src_id = class_id(fqcn)
            ctx = {"package": meta.get("package", ""), "imports": meta.get("imports", [])}

            parent_candidates = self._resolve_type_candidates(meta.get("extends"), ctx, class_catalog)
            for parent_fqcn in parent_candidates:
                dst_id = class_id(parent_fqcn)
                self.store.add_reference("IMPLEMENTS", "Class", src_id, "Class", dst_id, 0.8)
                for sig, method_id in class_methods.get(fqcn, {}).items():
                    parent_method = class_methods.get(parent_fqcn, {}).get(sig)
                    if parent_method:
                        self.store.add_reference("OVERRIDES", "Method", method_id, "Method", parent_method, 1.0)

            for iface in meta.get("interfaces", []):
                iface_candidates = self._resolve_type_candidates(iface, ctx, class_catalog)
                for iface_fqcn in iface_candidates:
                    dst_id = class_id(iface_fqcn)
                    self.store.add_reference("IMPLEMENTS", "Class", src_id, "Class", dst_id, 1.0)
                    for sig, method_id in class_methods.get(fqcn, {}).items():
                        iface_method = class_methods.get(iface_fqcn, {}).get(sig)
                        if iface_method:
                            self.store.add_reference("OVERRIDES", "Method", method_id, "Method", iface_method, 1.0)
