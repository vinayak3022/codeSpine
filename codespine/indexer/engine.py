from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from dataclasses import dataclass
from typing import Callable

from codespine.config import SETTINGS
from codespine.indexer.call_resolver import resolve_calls
from codespine.indexer.java_parser import parse_java_source
from codespine.indexer.symbol_builder import class_id, digest_bytes, file_id, method_id, symbol_id
from codespine.search.vector import embed_text

LOGGER = logging.getLogger(__name__)


def _parse_file_worker(file_path: str, root_path: str, project_id: str) -> dict:
    """Pure CPU/IO work – no DB access. Safe to run in a thread pool."""
    rel_path = os.path.relpath(file_path, root_path)
    is_test = "src/test/java" in file_path.replace("\\", "/")
    scope = JavaIndexer._scope_from_rel_path(rel_path)
    with open(file_path, "rb") as fh:
        source = fh.read()
    parsed = parse_java_source(source)
    f_id = file_id(project_id, rel_path)
    digest = digest_bytes(source)
    return {
        "file_path": file_path,
        "rel_path": rel_path,
        "source": source,
        "parsed": parsed,
        "f_id": f_id,
        "digest": digest,
        "is_test": is_test,
        "scope": scope,
    }


@dataclass
class IndexResult:
    project_id: str
    files_found: int
    files_indexed: int
    classes_indexed: int
    methods_indexed: int
    calls_resolved: int
    type_relationships: int
    embeddings_generated: int


class JavaIndexer:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def detect_projects_in_workspace(root_path: str) -> list[str]:
        """Detect independent projects under a workspace folder (e.g. ~/IdeaProjects/).

        A *workspace* is a directory that is not itself a project but contains
        multiple independent project directories (each with a .git dir or a build
        file at their root).  If root_path itself is a project, returns [root_path].

        Returns a list of project root directories (absolute paths).
        """
        root_path = os.path.abspath(root_path)
        build_markers = {
            "pom.xml", "build.gradle", "build.gradle.kts",
            "settings.gradle", "settings.gradle.kts",
        }
        skip = {".git", "target", "build", "out", ".idea", ".gradle", ".mvn", "node_modules"}

        # If the root itself looks like a project, it is the only project.
        has_git = os.path.isdir(os.path.join(root_path, ".git"))
        has_build = any(os.path.isfile(os.path.join(root_path, m)) for m in build_markers)
        if has_git or has_build:
            return [root_path]

        # Scan one level deep for project subdirectories.
        project_dirs: list[str] = []
        try:
            for entry in os.scandir(root_path):
                if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
                    continue
                sub_has_git = os.path.isdir(os.path.join(entry.path, ".git"))
                sub_has_build = any(
                    os.path.isfile(os.path.join(entry.path, m)) for m in build_markers
                )
                if sub_has_git or sub_has_build:
                    project_dirs.append(os.path.abspath(entry.path))
        except OSError:
            pass

        return sorted(project_dirs) if project_dirs else [root_path]

    @staticmethod
    def detect_modules(root_path: str) -> list[str]:
        """Detect Maven/Gradle module boundaries under root_path.

        Returns a list of module root directories (absolute paths).
        Returns [root_path] for single-module projects.
        """
        import re

        root_path = os.path.abspath(root_path)
        build_markers = {"pom.xml", "build.gradle", "build.gradle.kts"}

        # Maven multi-module: parent pom.xml with <modules> element
        parent_pom = os.path.join(root_path, "pom.xml")
        if os.path.isfile(parent_pom):
            try:
                with open(parent_pom, "rb") as f:
                    content = f.read().decode("utf-8", errors="replace")
                if "<modules>" in content:
                    raw_modules = re.findall(r"<module>(.*?)</module>", content)
                    found = []
                    for m in raw_modules:
                        m_path = os.path.join(root_path, m.strip())
                        if os.path.isdir(m_path):
                            found.append(os.path.abspath(m_path))
                    if found:
                        return found
            except OSError:
                pass

        # Gradle multi-project: subdirs with their own build file
        module_dirs: list[str] = []
        skip = {".git", "target", "build", "out", ".idea", ".gradle", ".mvn", "node_modules"}
        try:
            for entry in os.scandir(root_path):
                if not entry.is_dir() or entry.name.startswith(".") or entry.name in skip:
                    continue
                for marker in build_markers:
                    if os.path.isfile(os.path.join(entry.path, marker)):
                        module_dirs.append(os.path.abspath(entry.path))
                        break
        except OSError:
            pass

        if module_dirs:
            return sorted(module_dirs)

        return [root_path]

    def index_project(
        self,
        root_path: str,
        full: bool = True,
        progress: Callable[[str, dict], None] | None = None,
        project_id: str | None = None,
        embed: bool = True,
    ) -> IndexResult:
        root_path = os.path.abspath(root_path)
        if project_id is None:
            # Reuse the existing project ID for this path if one exists.
            # This prevents ID drift when the same module is re-indexed
            # directly (vision-server) vs from a workspace root (vision::vision-server).
            try:
                existing = self.store.query_records(
                    "MATCH (p:Project) WHERE p.path = $path RETURN p.id as id LIMIT 1",
                    {"path": root_path},
                )
                if existing:
                    project_id = existing[0]["id"]
            except Exception:
                pass
            if project_id is None:
                project_id = os.path.basename(root_path)
        current_files = self._collect_java_files(root_path)
        self._emit(progress, "scan_done", files_found=len(current_files))
        db_files = self.store.project_file_hashes(project_id) if not full else {}
        meta_cache = self._load_file_meta_cache(project_id)
        current_file_ids = {
            file_id(project_id, os.path.relpath(fp, root_path))
            for fp in current_files
        }

        if full:
            to_reindex = current_files
            deleted_file_ids = []
            meta_cache = {}
            # Wipe the embedding cache on a full re-index so stale embeddings
            # (including those from the old SQLite format) are not carried over.
            from codespine.search.vector import _CACHE as _embed_cache
            _embed_cache.clear()
        else:
            to_reindex, deleted_file_ids, meta_cache = self._plan_incremental(
                project_id,
                root_path,
                current_files,
                db_files,
                meta_cache,
            )
        self._emit(
            progress,
            "plan_done",
            files_to_index=len(to_reindex),
            deleted_files=len(deleted_file_ids),
            mode="full" if full else "incremental",
        )
        if not full and not to_reindex and not deleted_file_ids:
            self._prune_meta_cache(meta_cache, current_file_ids)
            self._save_file_meta_cache(project_id, meta_cache)
            self._emit(progress, "resolve_calls_done", calls_resolved=0)
            self._emit(progress, "resolve_types_done", type_relationships=0)
            return IndexResult(
                project_id=project_id,
                files_found=len(current_files),
                files_indexed=0,
                classes_indexed=0,
                methods_indexed=0,
                calls_resolved=0,
                type_relationships=0,
                embeddings_generated=0,
            )

        files_indexed = 0
        classes_indexed = 0
        methods_indexed = 0
        calls_resolved = 0
        type_relationships = 0
        file_batch_size = max(1, int(getattr(SETTINGS, "index_file_batch_size", 64)))
        edge_batch_size = max(1, int(getattr(SETTINGS, "edge_write_batch_size", 2000)))

        if not full:
            method_catalog, class_catalog, fqcn_to_class_ids, class_methods = (
                self._existing_catalogs(project_id)
            )
        else:
            method_catalog: dict[str, dict] = {}
            class_catalog: dict[str, list[str]] = {}
            fqcn_to_class_ids: dict[str, list[str]] = {}
            class_methods: dict[str, dict[str, str]] = {}
        method_calls: dict[str, list] = {}
        method_context: dict[str, dict] = {}
        class_meta: dict[str, dict] = {}
        di_classes: list[dict] = []  # accumulates DI metadata for resolver pass

        # ── Parallel parse (CPU/IO) ──────────────────────────────────────────
        # tree-sitter releases the GIL so ThreadPoolExecutor gives real speedup.
        _workers = max(1, min(8, len(to_reindex), os.cpu_count() or 4))
        parse_results: list[dict] = []
        if to_reindex:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_workers) as ex:
                futs = {
                    ex.submit(_parse_file_worker, fp, root_path, project_id): fp
                    for fp in to_reindex
                }
                done = 0
                for fut in concurrent.futures.as_completed(futs):
                    done += 1
                    fp = futs[fut]
                    try:
                        parse_results.append(fut.result())
                    except Exception as exc:
                        LOGGER.warning("Skipping %s: %s", fp, exc)
                    self._emit(
                        progress,
                        "parse_progress",
                        indexed=done,
                        total=len(to_reindex),
                        file_path=fp,
                    )

        # ── Chunked DB writes ─────────────────────────────────────────────────
        if full:
            self.store.clear_project(project_id)
        elif deleted_file_ids:
            for delete_chunk in self._chunked(deleted_file_ids, file_batch_size):
                with self.store.transaction():
                    for fid in delete_chunk:
                        self.store.clear_file(fid)
                self.store._recycle_conn()

        # Clean up stale project entries that point to the same path under a
        # different ID (e.g. re-indexing "vision-server" directly after it was
        # previously indexed as "vision::vision-server" from a workspace root).
        try:
            stale = self.store.query_records(
                "MATCH (p:Project) WHERE p.path = $path AND p.id <> $pid RETURN p.id as id",
                {"path": root_path, "pid": project_id},
            )
            for old in stale:
                self.store.clear_project(old["id"])
        except Exception:
            pass  # best-effort cleanup

        self.store.upsert_project(project_id, root_path)

        for parse_chunk in self._chunked(parse_results, file_batch_size):
            file_rows: list[dict] = []
            class_rows: list[dict] = []
            method_rows: list[dict] = []
            symbol_rows: list[dict] = []

            for pr in parse_chunk:
                file_path = pr["file_path"]
                parsed = pr["parsed"]
                f_id = pr["f_id"]
                file_digest = pr["digest"]
                is_test = pr["is_test"]
                scope = pr["scope"]
                source = pr["source"]

                file_rows.append(
                    {
                        "id": f_id,
                        "path": file_path,
                        "project_id": project_id,
                        "is_test": is_test,
                        "hash": file_digest,
                    }
                )
                self._update_meta_cache_entry(meta_cache, f_id, file_path, file_digest, len(source), imports=parsed.imports)

                for cls in parsed.classes:
                    c_id = class_id(cls.fqcn, scope)
                    class_rows.append(
                        {
                            "id": c_id,
                            "fqcn": cls.fqcn,
                            "name": cls.name,
                            "package": cls.package,
                            "file_id": f_id,
                        }
                    )
                    class_catalog.setdefault(cls.name, [])
                    if cls.fqcn not in class_catalog[cls.name]:
                        class_catalog[cls.name].append(cls.fqcn)
                    fqcn_to_class_ids.setdefault(cls.fqcn, [])
                    if c_id not in fqcn_to_class_ids[cls.fqcn]:
                        fqcn_to_class_ids[cls.fqcn].append(c_id)
                    class_meta[c_id] = {
                        "id": c_id,
                        "fqcn": cls.fqcn,
                        "package": parsed.package,
                        "imports": parsed.imports,
                        "extends": cls.extends,
                        "interfaces": cls.interfaces,
                        "annotations": cls.annotations,
                        "scope": scope,
                    }
                    class_methods.setdefault(c_id, {})

                    cls_symbol_id = symbol_id("class", cls.fqcn, scope)
                    symbol_rows.append(
                        {
                            "id": cls_symbol_id,
                            "kind": "class",
                            "name": cls.name,
                            "fqname": cls.fqcn,
                            "file_id": f_id,
                            "line": cls.line,
                            "col": cls.col,
                            "embedding": embed_text(f"class {cls.fqcn}") if embed else None,
                        }
                    )
                    classes_indexed += 1

                    # Collect DI metadata for the resolver pass.
                    di_cls_entry: dict = {
                        "id": c_id,
                        "fqcn": cls.fqcn,
                        "name": cls.name,
                        "package": cls.package,
                        "annotations": cls.annotations,
                        "injected_fields": [
                            {
                                "name": f.name,
                                "type_name": f.type_name,
                                "injection_annotation": f.injection_annotation,
                                "qualifier": f.qualifier,
                            }
                            for f in cls.fields
                            if f.injection_annotation
                        ],
                        "methods_with_provides": [
                            {
                                "name": m.name,
                                "provides_type": m.provides_type,
                                "provides_annotation": next(
                                    (a for a in m.annotations if a.split(".")[-1] in {"Provides", "Bean"}),
                                    "Provides",
                                ),
                            }
                            for m in cls.methods
                            if m.provides_type
                        ],
                    }
                    di_classes.append(di_cls_entry)

                    for fld in cls.fields:
                        fqfield = f"{cls.fqcn}#{fld.name}"
                        symbol_rows.append(
                            {
                                "id": symbol_id("field", fqfield, scope),
                                "kind": "field",
                                "name": fld.name,
                                "fqname": fqfield,
                                "file_id": f_id,
                                "line": fld.line,
                                "col": fld.col,
                                "embedding": embed_text(f"field {fqfield} {fld.type_name}") if embed else None,
                            }
                        )

                    for method in cls.methods:
                        m_id = method_id(cls.fqcn, method.signature, scope)
                        method_rows.append(
                            {
                                "id": m_id,
                                "class_id": c_id,
                                "name": method.name,
                                "signature": method.signature,
                                "return_type": method.return_type,
                                "modifiers": method.modifiers + [f"@{a}" for a in method.annotations],
                                "is_constructor": method.name == cls.name,
                                "is_test": is_test,
                            }
                        )

                        fqname = f"{cls.fqcn}#{method.signature}"
                        symbol_rows.append(
                            {
                                "id": symbol_id("method", fqname, scope),
                                "kind": "method",
                                "name": method.name,
                                "fqname": fqname,
                                "file_id": f_id,
                                "line": method.line,
                                "col": method.col,
                                "embedding": embed_text(f"method {fqname} returns {method.return_type}") if embed else None,
                            }
                        )
                        methods_indexed += 1

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
                files_indexed += 1

            # For incremental re-indexes clear files in bulk first, then use
            # CREATE (not MERGE) for all writes — after clear the nodes are
            # guaranteed absent so we skip the costly existence-check MERGE pays.
            if not full:
                for clear_sub in self._chunked([r["id"] for r in file_rows], 100):
                    with self.store.transaction():
                        self.store.clear_files_batch(clear_sub)
                    self.store._recycle_conn()
            with self.store.transaction():
                self.store.upsert_files_batch(file_rows)
            self.store._recycle_conn()
            with self.store.transaction():
                self.store.upsert_classes_batch(class_rows)
            self.store._recycle_conn()
            _METHOD_SUB_BATCH = 200
            for method_sub in self._chunked(method_rows, _METHOD_SUB_BATCH):
                with self.store.transaction():
                    self.store.upsert_methods_batch(method_sub)
                self.store._recycle_conn()
            _SYMBOL_SUB_BATCH = 200
            for symbol_sub in self._chunked(symbol_rows, _SYMBOL_SUB_BATCH):
                with self.store.transaction():
                    self.store.upsert_symbols_batch(symbol_sub)
                self.store._recycle_conn()

        self._emit(progress, "resolve_calls_start")
        call_rows: list[dict] = []
        for src, dst, confidence, reason in resolve_calls(method_catalog, method_calls, method_context, class_catalog):
            call_rows.append(
                {
                    "source_id": src,
                    "target_id": dst,
                    "confidence": confidence,
                    "reason": reason,
                }
            )
        for call_chunk in self._chunked(call_rows, edge_batch_size):
            with self.store.transaction():
                self.store.add_calls_batch(call_chunk)
            calls_resolved += len(call_chunk)
            self.store._recycle_conn()
            self._emit(progress, "resolve_calls_progress", calls_resolved=calls_resolved)
        self._emit(progress, "resolve_calls_done", calls_resolved=calls_resolved)

        self._emit(progress, "resolve_types_start")
        type_rows = self._build_inheritance_edges(
            class_meta,
            class_catalog,
            class_methods,
            fqcn_to_class_ids,
        )
        for rel_chunk in self._chunked(type_rows, edge_batch_size):
            with self.store.transaction():
                self.store.add_references_batch(rel_chunk)
            type_relationships += len(rel_chunk)
            self.store._recycle_conn()
        self._emit(progress, "resolve_types_done", type_relationships=type_relationships)

        # ── DI binding resolution ─────────────────────────────────────────────
        # Resolve @Inject/@Autowired/@Provides/@Bean bindings into INJECTS and
        # BINDS_INTERFACE graph edges.  This runs after all classes/methods are
        # written so the full cross-project catalog is available for type lookup.
        di_inject_rows: list[dict] = []
        di_bind_rows: list[dict] = []
        try:
            from codespine.indexer.di_resolver import resolve_di_bindings
            for tup in resolve_di_bindings(class_catalog, class_meta, di_classes, fqcn_to_class_ids):
                src, dst, fw_or_conf, bt_or_reason, conf, edge_type = tup
                if edge_type == "INJECTS":
                    di_inject_rows.append({
                        "src": src, "dst": dst,
                        "framework": fw_or_conf, "binding_type": bt_or_reason,
                        "confidence": conf,
                    })
                else:
                    di_bind_rows.append({
                        "src": src, "dst": dst,
                        "confidence": float(fw_or_conf), "reason": bt_or_reason,
                    })
        except Exception as exc:
            LOGGER.warning("DI resolver failed (non-fatal): %s", exc)

        _DI_BATCH = 500
        for chunk in self._chunked(di_inject_rows, _DI_BATCH):
            with self.store.transaction():
                self.store.add_injections_batch(chunk)
            self.store._recycle_conn()
        for chunk in self._chunked(di_bind_rows, _DI_BATCH):
            with self.store.transaction():
                self.store.add_interface_bindings_batch(chunk)
            self.store._recycle_conn()
        self._emit(progress, "di_done",
                   injections=len(di_inject_rows), interface_bindings=len(di_bind_rows))

        self._prune_meta_cache(meta_cache, current_file_ids)
        self._save_file_meta_cache(project_id, meta_cache)

        return IndexResult(
            project_id=project_id,
            files_found=len(current_files),
            files_indexed=files_indexed,
            classes_indexed=classes_indexed,
            methods_indexed=methods_indexed,
            calls_resolved=calls_resolved,
            type_relationships=type_relationships,
            embeddings_generated=classes_indexed + methods_indexed if embed else 0,
        )

    @staticmethod
    def _collect_java_files(root_path: str) -> list[str]:
        out: list[str] = []
        skip_dirs = {".git", "target", "build", "out", ".idea", ".gradle", ".mvn", "node_modules"}
        for root, dirs, files in os.walk(root_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            normalized = root.replace("\\", "/")
            if "/src/" not in normalized and not normalized.endswith("/src"):
                continue
            for filename in files:
                if filename.endswith(".java"):
                    out.append(os.path.join(root, filename))
        return out

    def _plan_incremental(
        self,
        project_id: str,
        root_path: str,
        files: list[str],
        db_files: dict[str, dict[str, str]],
        meta_cache: dict[str, dict],
    ) -> tuple[list[str], list[str], dict[str, dict]]:
        current_ids = {
            file_id(project_id, os.path.relpath(fp, root_path))
            for fp in files
        }
        deleted_file_ids = [fid for fid in db_files if fid not in current_ids]
        to_reindex: list[str] = []

        for file_path in files:
            rel_path = os.path.relpath(file_path, root_path)
            fid = file_id(project_id, rel_path)
            old_hash = db_files.get(fid, {}).get("hash")
            try:
                st = os.stat(file_path)
            except OSError:
                continue
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
            size = int(st.st_size)
            cached = meta_cache.get(fid, {})

            if (
                cached
                and int(cached.get("mtime_ns", -1)) == mtime_ns
                and int(cached.get("size", -1)) == size
                and cached.get("hash")
                and cached.get("hash") == old_hash
            ):
                continue

            with open(file_path, "rb") as f:
                digest = digest_bytes(f.read())
            meta_cache[fid] = {"mtime_ns": mtime_ns, "size": size, "hash": digest}
            if old_hash != digest:
                to_reindex.append(file_path)

        for fid in deleted_file_ids:
            meta_cache.pop(fid, None)

        return to_reindex, deleted_file_ids, meta_cache

    def _existing_catalogs(
        self, project_id: str
    ) -> tuple[
        dict[str, dict],          # method_catalog   (method_id → meta)
        dict[str, list[str]],     # class_catalog    (class_name → [fqcn, ...])
        dict[str, list[str]],     # fqcn_to_class_ids (fqcn → [class_id, ...])
        dict[str, dict[str, str]] # class_methods    (class_id → {sig → method_id})
    ]:
        """Load all four incremental-index catalogs in a single DB round-trip."""
        recs = self.store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            RETURN m.id as method_id, m.name as mname, m.signature as signature,
                   c.fqcn as class_fqcn, c.name as cname, c.id as class_id
            """,
            {"pid": project_id},
        )
        method_catalog: dict[str, dict] = {}
        class_catalog: dict[str, list[str]] = {}
        fqcn_to_class_ids: dict[str, list[str]] = {}
        class_methods: dict[str, dict[str, str]] = {}
        seen_fqcn_cid: set[tuple[str, str]] = set()
        seen_name_fqcn: set[tuple[str, str]] = set()

        for r in recs:
            mid = r["method_id"]
            sig = r.get("signature") or ""
            arg_str = sig[sig.find("(") + 1: sig.rfind(")")] if "(" in sig and ")" in sig else ""
            param_count = 0 if not arg_str else arg_str.count(",") + 1
            cid = r.get("class_id", "")
            fqcn = r.get("class_fqcn", "")
            cname = r.get("cname", "")

            method_catalog[mid] = {
                "signature": sig,
                "name": r.get("mname", ""),
                "param_count": param_count,
                "class_fqcn": fqcn,
                "class_id": cid,
            }

            if fqcn and cid:
                key_fc = (fqcn, cid)
                if key_fc not in seen_fqcn_cid:
                    seen_fqcn_cid.add(key_fc)
                    fqcn_to_class_ids.setdefault(fqcn, [])
                    if cid not in fqcn_to_class_ids[fqcn]:
                        fqcn_to_class_ids[fqcn].append(cid)

                key_nc = (cname, fqcn)
                if key_nc not in seen_name_fqcn:
                    seen_name_fqcn.add(key_nc)
                    class_catalog.setdefault(cname, [])
                    if fqcn not in class_catalog[cname]:
                        class_catalog[cname].append(fqcn)

            if cid:
                class_methods.setdefault(cid, {})
                class_methods[cid][sig] = mid

        return method_catalog, class_catalog, fqcn_to_class_ids, class_methods

    def _existing_method_catalog(self, project_id: str) -> dict[str, dict]:
        recs = self.store.query_records(
            """
            MATCH (m:Method), (c:Class), (f:File)
            WHERE m.class_id = c.id AND c.file_id = f.id AND f.project_id = $pid
            RETURN m.id as method_id, m.name as name, m.signature as signature, c.fqcn as class_fqcn, c.id as class_id
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
                "class_id": r.get("class_id", ""),
            }
        return out

    def _existing_class_ids_by_fqcn(self, project_id: str) -> dict[str, list[str]]:
        recs = self.store.query_records(
            """
            MATCH (c:Class), (f:File)
            WHERE c.file_id = f.id AND f.project_id = $pid
            RETURN c.fqcn as fqcn, c.id as class_id
            """,
            {"pid": project_id},
        )
        out: dict[str, list[str]] = {}
        for r in recs:
            fqcn = r.get("fqcn", "")
            cid = r.get("class_id", "")
            if not fqcn or not cid:
                continue
            out.setdefault(fqcn, [])
            if cid not in out[fqcn]:
                out[fqcn].append(cid)
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
            RETURN c.id as class_id, m.signature as signature, m.id as method_id
            """,
            {"pid": project_id},
        )
        out: dict[str, dict[str, str]] = {}
        for r in recs:
            class_key = r.get("class_id")
            if not class_key:
                continue
            out.setdefault(class_key, {})
            out[class_key][r["signature"]] = r["method_id"]
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
        fqcn_to_class_ids: dict[str, list[str]],
    ) -> list[dict]:
        rel_rows: list[dict] = []
        for src_id, meta in class_meta.items():
            ctx = {"package": meta.get("package", ""), "imports": meta.get("imports", [])}

            parent_candidates = self._resolve_type_candidates(meta.get("extends"), ctx, class_catalog)
            for parent_fqcn in parent_candidates:
                for dst_id in fqcn_to_class_ids.get(parent_fqcn, []):
                    rel_rows.append(
                        {
                            "rel": "IMPLEMENTS",
                            "src_label": "Class",
                            "src_id": src_id,
                            "dst_label": "Class",
                            "dst_id": dst_id,
                            "confidence": 0.8,
                        }
                    )
                    for sig, method_id in class_methods.get(src_id, {}).items():
                        parent_method = class_methods.get(dst_id, {}).get(sig)
                        if parent_method:
                            rel_rows.append(
                                {
                                    "rel": "OVERRIDES",
                                    "src_label": "Method",
                                    "src_id": method_id,
                                    "dst_label": "Method",
                                    "dst_id": parent_method,
                                    "confidence": 1.0,
                                }
                            )

            for iface in meta.get("interfaces", []):
                iface_candidates = self._resolve_type_candidates(iface, ctx, class_catalog)
                for iface_fqcn in iface_candidates:
                    for dst_id in fqcn_to_class_ids.get(iface_fqcn, []):
                        rel_rows.append(
                            {
                                "rel": "IMPLEMENTS",
                                "src_label": "Class",
                                "src_id": src_id,
                                "dst_label": "Class",
                                "dst_id": dst_id,
                                "confidence": 1.0,
                            }
                        )
                        for sig, method_id in class_methods.get(src_id, {}).items():
                            iface_method = class_methods.get(dst_id, {}).get(sig)
                            if iface_method:
                                rel_rows.append(
                                    {
                                        "rel": "OVERRIDES",
                                        "src_label": "Method",
                                        "src_id": method_id,
                                        "dst_label": "Method",
                                        "dst_id": iface_method,
                                        "confidence": 1.0,
                                    }
                                )
        return rel_rows

    @staticmethod
    def _chunked(items: list, size: int):
        for i in range(0, len(items), size):
            yield items[i : i + size]

    @staticmethod
    def _meta_cache_path(project_id: str) -> str:
        base = SETTINGS.index_meta_dir
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            base = "/tmp/.codespine_index_meta"
            os.makedirs(base, exist_ok=True)
        return os.path.join(base, f"{project_id}.json")

    def _load_file_meta_cache(self, project_id: str) -> dict[str, dict]:
        path = self._meta_cache_path(project_id)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError, TypeError):
            return {}
        return {}

    def _save_file_meta_cache(self, project_id: str, data: dict[str, dict]) -> None:
        path = self._meta_cache_path(project_id)
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp_path, path)
        except OSError:
            return

    @staticmethod
    def _update_meta_cache_entry(
        meta_cache: dict[str, dict], fid: str, file_path: str, digest: str, size_hint: int,
        imports: list[str] | None = None,
    ) -> None:
        try:
            st = os.stat(file_path)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
            size = int(st.st_size)
        except OSError:
            mtime_ns = -1
            size = size_hint
        entry: dict = {"mtime_ns": mtime_ns, "size": size, "hash": digest}
        if imports is not None:
            entry["imports"] = imports
        meta_cache[fid] = entry

    @staticmethod
    def _prune_meta_cache(meta_cache: dict[str, dict], current_file_ids: set[str]) -> None:
        for fid in list(meta_cache.keys()):
            if fid not in current_file_ids:
                del meta_cache[fid]

    @staticmethod
    def _emit(progress: Callable[[str, dict], None] | None, event: str, **payload: object) -> None:
        if progress is None:
            return
        progress(event, payload)

    @staticmethod
    def _scope_from_rel_path(rel_path: str) -> str:
        normalized = rel_path.replace("\\", "/")
        if "/java/" in normalized:
            return normalized.split("/java/", 1)[0]
        if "/src/" in normalized:
            return normalized.split("/src/", 1)[0]
        scope = os.path.dirname(normalized).strip()
        return scope or "."

    @staticmethod
    def detect_unresolved_imports(store) -> dict[str, list[str]]:
        """Detect imports that reference packages not covered by any indexed project.

        Returns a dict mapping unresolved base packages (e.g. "com.foo.bar")
        to a list of sample import FQCNs.  Useful for suggesting which sibling
        projects to index.

        Only reports project-internal packages (not java.*, javax.*, org.apache.*
        etc.).
        """
        # 1. Collect all indexed class FQCNs
        try:
            recs = store.query_records("MATCH (c:Class) RETURN c.fqcn as fqcn")
        except Exception:
            return {}
        indexed_fqcns = {r["fqcn"] for r in recs if r.get("fqcn")}
        indexed_packages = set()
        for fqcn in indexed_fqcns:
            parts = fqcn.rsplit(".", 1)
            if len(parts) == 2:
                indexed_packages.add(parts[0])

        # 2. Collect all imports from overlay + any stored file data
        # Parse imports from the parsed file metadata if available
        meta_dir = SETTINGS.index_meta_dir
        all_imports: set[str] = set()
        if os.path.isdir(meta_dir):
            for fname in os.listdir(meta_dir):
                if not fname.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(meta_dir, fname), "r") as f:
                        data = json.load(f)
                    for fid, fmeta in data.items():
                        for imp in fmeta.get("imports", []):
                            all_imports.add(imp)
                except Exception:
                    pass

        # 3. Also scan the DB for CALLS edges that reference unknown targets
        # (lightweight — just check which classes were resolved vs not)

        # 4. Filter: skip standard library / well-known third-party packages
        _SKIP_PREFIXES = (
            "java.", "javax.", "jakarta.",
            "org.apache.", "org.springframework.", "org.hibernate.",
            "org.slf4j.", "org.junit.", "org.mockito.",
            "com.google.", "com.fasterxml.", "com.sun.",
            "io.micrometer.", "io.netty.", "io.lettuce.",
            "lombok.", "reactor.", "rx.",
        )

        unresolved: dict[str, list[str]] = {}
        for imp in all_imports:
            if any(imp.startswith(prefix) for prefix in _SKIP_PREFIXES):
                continue
            # Check if this import's class exists in the index
            simple_name = imp.rsplit(".", 1)[-1]
            pkg = imp.rsplit(".", 1)[0] if "." in imp else ""
            if imp in indexed_fqcns:
                continue
            if pkg in indexed_packages:
                continue  # same package, just not this specific class
            # Group by top 3 package segments
            parts = imp.split(".")
            base_pkg = ".".join(parts[:min(3, len(parts))])
            if base_pkg not in unresolved:
                unresolved[base_pkg] = []
            if len(unresolved[base_pkg]) < 5:
                unresolved[base_pkg].append(imp)
        return unresolved
