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
        self.store.upsert_project(project_id, root_path)

        if full:
            self.store.clear_project(project_id)

        files_indexed = 0
        classes_indexed = 0
        methods_indexed = 0

        method_catalog: dict[str, str] = {}
        method_calls: dict[str, list[str]] = {}

        for root, _, files in os.walk(root_path):
            if "src" not in root:
                continue
            if any(skip in root for skip in ["target", "build", "out", ".git"]):
                continue
            for filename in files:
                if not filename.endswith(".java"):
                    continue
                file_path = os.path.join(root, filename)
                rel_path = os.path.relpath(file_path, root_path)
                is_test = "src/test/java" in file_path.replace("\\", "/")

                with open(file_path, "rb") as f:
                    source = f.read()

                parsed = parse_java_source(source)
                f_id = file_id(project_id, rel_path)
                self.store.upsert_file(f_id, file_path, project_id, is_test, digest_bytes(source))

                for cls in parsed.classes:
                    c_id = class_id(cls.fqcn)
                    self.store.upsert_class(c_id, cls.fqcn, cls.name, cls.package, f_id)

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
                            modifiers=method.modifiers,
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

                        method_catalog[m_id] = method.signature
                        method_calls[m_id] = method.calls
                files_indexed += 1

        for src, dst, confidence, reason in resolve_calls(method_catalog, method_calls):
            self.store.add_call(src, dst, confidence, reason)

        return IndexResult(
            project_id=project_id,
            files_indexed=files_indexed,
            classes_indexed=classes_indexed,
            methods_indexed=methods_indexed,
        )
