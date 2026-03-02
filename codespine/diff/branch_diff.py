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


def _symbol_manifest(repo_path: str) -> dict[str, dict]:
    manifest: dict[str, dict] = {}
    for root, _, files in os.walk(repo_path):
        if any(skip in root for skip in [".git", "target", "build", "out"]):
            continue
        for f in files:
            if not f.endswith(".java"):
                continue
            path = os.path.join(root, f)
            rel = os.path.relpath(path, repo_path)
            with open(path, "rb") as fp:
                source = fp.read()
            parsed = parse_java_source(source)
            method_hashes = _method_hashes(source)
            class_hashes = _class_hashes(source)
            for cls in parsed.classes:
                cls_key = f"class:{cls.fqcn}"
                manifest[cls_key] = {
                    "kind": "Class",
                    "file": rel,
                    "name": cls.fqcn,
                    "hash": class_hashes.get(cls.name, cls.body_hash),
                    "line_start": cls.line,
                }
                for m in cls.methods:
                    m_key = f"method:{cls.fqcn}#{m.signature}"
                    mh = method_hashes.get(f"{m.name}({','.join(m.parameter_types)})") or method_hashes.get(m.signature) or {}
                    manifest[m_key] = {
                        "kind": "Method",
                        "file": rel,
                        "name": m.signature,
                        "class": cls.fqcn,
                        "hash": m.body_hash or mh.get("hash"),
                        "line_start": mh.get("line_start", m.line),
                        "line_end": mh.get("line_end", m.line),
                    }
    return manifest


def compare_branches(repo_path: str, base_ref: str, head_ref: str) -> dict:
    temp_dir = tempfile.mkdtemp(prefix="codespine-diff-")
    base_dir = os.path.join(temp_dir, "base")
    head_dir = os.path.join(temp_dir, "head")

    try:
        subprocess.run(["git", "-C", repo_path, "worktree", "add", "--detach", base_dir, base_ref], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "worktree", "add", "--detach", head_dir, head_ref], check=True, capture_output=True)

        base_manifest = _symbol_manifest(base_dir)
        head_manifest = _symbol_manifest(head_dir)

        added = sorted(set(head_manifest) - set(base_manifest))
        removed = sorted(set(base_manifest) - set(head_manifest))

        modified = []
        for key in sorted(set(base_manifest) & set(head_manifest)):
            if json.dumps(base_manifest[key], sort_keys=True) != json.dumps(head_manifest[key], sort_keys=True):
                modified.append(key)

        return {
            "base": base_ref,
            "head": head_ref,
            "added": [head_manifest[k] for k in added],
            "removed": [base_manifest[k] for k in removed],
            "modified": [head_manifest[k] for k in modified],
        }
    finally:
        subprocess.run(["git", "-C", repo_path, "worktree", "remove", "--force", base_dir], check=False, capture_output=True)
        subprocess.run(["git", "-C", repo_path, "worktree", "remove", "--force", head_dir], check=False, capture_output=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
