from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser, Query

from codespine.indexer.java_parser import parse_java_source

JAVA_LANGUAGE = Language(tsjava.language())
PARSER = Parser(JAVA_LANGUAGE)


def _text(node) -> str:
    return node.text.decode("utf-8")


def _captures(query: Query, node) -> list[tuple]:
    if hasattr(query, "captures"):
        return query.captures(node)

    from tree_sitter import QueryCursor

    raw = None
    try:
        cursor = QueryCursor(query)
        if hasattr(cursor, "captures"):
            raw = cursor.captures(node)
    except TypeError:
        raw = None

    if raw is None:
        cursor = QueryCursor()
        for call in (
            lambda: cursor.captures(query, node),
            lambda: cursor.captures(node, query),
        ):
            try:
                raw = call()
                break
            except TypeError:
                continue
    if raw is None:
        return []
    if isinstance(raw, dict):
        out: list[tuple] = []
        for tag, nodes in raw.items():
            for n in nodes:
                out.append((n, tag))
        return out
    out: list[tuple] = []
    for item in raw:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        n, t = item[0], item[1]
        if isinstance(t, int):
            tag = None
            for attr in ("capture_name_for_id", "capture_name"):
                if hasattr(query, attr):
                    try:
                        tag = getattr(query, attr)(t)
                        break
                    except Exception:
                        pass
            out.append((n, tag if tag else str(t)))
        else:
            out.append((n, t))
    return out


def _hash_text(text: str) -> str:
    return hashlib.sha1(_normalize_java_snippet(text).encode("utf-8")).hexdigest()


def _normalize_java_snippet(text: str) -> str:
    """Normalize formatting/comments so branch diff emphasizes semantic edits."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*?$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*([{}();,])\s*", r"\1", text)
    return text


def _method_hashes(source: bytes) -> dict[str, dict]:
    tree = PARSER.parse(source)
    root = tree.root_node
    method_query = Query(
        JAVA_LANGUAGE,
        """
        [
          (method_declaration
            name: (identifier) @name
            parameters: (formal_parameters) @params) @decl
          (constructor_declaration
            name: (identifier) @name
            parameters: (formal_parameters) @params) @decl
        ]
        """,
    )
    methods: dict[str, dict] = {}
    grouped: dict[object, dict[str, str]] = {}
    for node, tag in _captures(method_query, root):
        key_node = node if tag == "decl" else node.parent
        grouped.setdefault(key_node, {})[tag] = _text(node)

    for node, capture in grouped.items():
        name = capture.get("name")
        params = capture.get("params", "()")
        if not name:
            continue
        signature = f"{name}{params}"
        methods[signature] = {
            "hash": _hash_text(_text(node)),
            "line_start": node.start_point[0] + 1,
            "line_end": node.end_point[0] + 1,
        }
    return methods


def _class_hashes(source: bytes) -> dict[str, str]:
    tree = PARSER.parse(source)
    root = tree.root_node
    class_query = Query(
        JAVA_LANGUAGE,
        """
        (class_declaration
          name: (identifier) @name) @decl
        """,
    )
    grouped: dict[object, dict[str, str]] = {}
    for node, tag in _captures(class_query, root):
        key_node = node if tag == "decl" else node.parent
        grouped.setdefault(key_node, {})[tag] = _text(node)
    out: dict[str, str] = {}
    for node, capture in grouped.items():
        name = capture.get("name")
        if name:
            out[name] = _hash_text(_text(node))
    return out


def _manifest_for_file(repo_path: str, rel_path: str) -> tuple[list[dict], list[str]]:
    path = os.path.join(repo_path, rel_path)
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, "rb") as fp:
            source = fp.read()
        parsed = parse_java_source(source)
        method_hashes = _method_hashes(source)
        class_hashes = _class_hashes(source)
    except Exception as exc:
        return [], [f"{rel_path}: {exc}"]

    records: list[dict] = []
    for cls in parsed.classes:
        class_semantic_id = f"class:{cls.fqcn}"
        class_hash = class_hashes.get(cls.name, cls.body_hash)
        records.append(
            {
                "kind": "Class",
                "file": rel_path,
                "name": cls.fqcn,
                "class": cls.fqcn,
                "semantic_id": class_semantic_id,
                "fqid": f"{class_semantic_id}@{rel_path}",
                "semantic_hash": class_hash,
                "hash": class_hash,
                "line_start": cls.line,
            }
        )
        for m in cls.methods:
            method_semantic_id = f"method:{cls.fqcn}#{m.signature}"
            mh = method_hashes.get(f"{m.name}({','.join(m.parameter_types)})") or method_hashes.get(m.signature) or {}
            method_hash = m.body_hash or mh.get("hash") or ""
            records.append(
                {
                    "kind": "Method",
                    "file": rel_path,
                    "name": m.signature,
                    "class": cls.fqcn,
                    "semantic_id": method_semantic_id,
                    "fqid": f"{method_semantic_id}@{rel_path}",
                    "semantic_hash": method_hash,
                    "hash": method_hash,
                    "line_start": mh.get("line_start", m.line),
                    "line_end": mh.get("line_end", m.line),
                }
            )
    return records, []


def _git_changed_files(repo_path: str, base_ref: str, head_ref: str) -> list[dict[str, str]]:
    proc = subprocess.run(
        ["git", "-C", repo_path, "diff", "--name-status", "--find-renames", base_ref, head_ref],
        check=True,
        capture_output=True,
        text=True,
    )
    changes: list[dict[str, str]] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            if len(parts) < 3:
                continue
            changes.append({"status": "M", "base": parts[1], "head": parts[2]})
        elif status == "A" and len(parts) >= 2:
            changes.append({"status": "A", "head": parts[1]})
        elif status == "D" and len(parts) >= 2:
            changes.append({"status": "D", "base": parts[1]})
        elif len(parts) >= 2:
            changes.append({"status": "M", "base": parts[1], "head": parts[1]})
    return changes


def _group_by_semantic_id(records: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for rec in records:
        grouped.setdefault(str(rec.get("semantic_id") or ""), []).append(rec)
    return grouped


def _diff_semantic_records(base_records: list[dict], head_records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    added: list[dict] = []
    removed: list[dict] = []
    modified: list[dict] = []

    base_groups = _group_by_semantic_id(base_records)
    head_groups = _group_by_semantic_id(head_records)
    semantic_ids = sorted((set(base_groups) | set(head_groups)) - {""})
    for semantic_id in semantic_ids:
        base_group = base_groups.get(semantic_id, [])
        head_group = head_groups.get(semantic_id, [])
        if len(base_group) == 1 and len(head_group) == 1:
            if str(base_group[0].get("semantic_hash") or "") != str(head_group[0].get("semantic_hash") or ""):
                modified.append(head_group[0])
            continue

        base_by_fqid = {str(rec.get("fqid") or ""): rec for rec in base_group}
        head_by_fqid = {str(rec.get("fqid") or ""): rec for rec in head_group}
        for fqid in sorted(set(head_by_fqid) - set(base_by_fqid)):
            added.append(head_by_fqid[fqid])
        for fqid in sorted(set(base_by_fqid) - set(head_by_fqid)):
            removed.append(base_by_fqid[fqid])
        for fqid in sorted(set(base_by_fqid) & set(head_by_fqid)):
            if str(base_by_fqid[fqid].get("semantic_hash") or "") != str(head_by_fqid[fqid].get("semantic_hash") or ""):
                modified.append(head_by_fqid[fqid])
    return added, removed, modified


def compare_branches(repo_path: str, base_ref: str, head_ref: str) -> dict:
    temp_dir = tempfile.mkdtemp(prefix="codespine-diff-")
    base_dir = os.path.join(temp_dir, "base")
    head_dir = os.path.join(temp_dir, "head")

    try:
        subprocess.run(["git", "-C", repo_path, "worktree", "add", "--detach", base_dir, base_ref], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "worktree", "add", "--detach", head_dir, head_ref], check=True, capture_output=True)

        changes = _git_changed_files(repo_path, base_ref, head_ref)
        added: list[dict] = []
        removed: list[dict] = []
        modified: list[dict] = []
        warnings: list[str] = []

        for change in changes:
            status = change["status"]
            base_rel = change.get("base")
            head_rel = change.get("head")
            base_records, base_warnings = _manifest_for_file(base_dir, base_rel) if base_rel else ([], [])
            head_records, head_warnings = _manifest_for_file(head_dir, head_rel) if head_rel else ([], [])
            for warning in base_warnings + head_warnings:
                if warning not in warnings:
                    warnings.append(warning)
            if base_warnings or head_warnings:
                continue
            if status == "A":
                added.extend(head_records)
            elif status == "D":
                removed.extend(base_records)
            else:
                file_added, file_removed, file_modified = _diff_semantic_records(base_records, head_records)
                added.extend(file_added)
                removed.extend(file_removed)
                modified.extend(file_modified)

        result = {
            "base": base_ref,
            "head": head_ref,
            "added": added,
            "removed": removed,
            "modified": modified,
        }
        if warnings:
            result["warnings"] = warnings
        return result
    finally:
        subprocess.run(["git", "-C", repo_path, "worktree", "remove", "--force", base_dir], check=False, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "worktree", "remove", "--force", head_dir], check=False, capture_output=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
