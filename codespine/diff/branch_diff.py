from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from codespine.indexer.java_parser import parse_java_source


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
                parsed = parse_java_source(fp.read())
            for cls in parsed.classes:
                cls_key = f"class:{cls.fqcn}"
                manifest[cls_key] = {"kind": "Class", "file": rel, "name": cls.fqcn}
                for m in cls.methods:
                    m_key = f"method:{cls.fqcn}#{m.signature}"
                    manifest[m_key] = {
                        "kind": "Method",
                        "file": rel,
                        "name": m.signature,
                        "class": cls.fqcn,
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
