from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("tree_sitter_java")

from codespine.diff import branch_diff


def test_diff_semantic_records_ignores_non_semantic_changes():
    base = [
        {
            "kind": "Method",
            "file": "src/App.java",
            "name": "run()",
            "semantic_id": "method:com.example.App#run()",
            "fqid": "method:com.example.App#run()@src/App.java",
            "semantic_hash": "abc123",
            "hash": "abc123",
            "line_start": 10,
            "line_end": 20,
        }
    ]
    head = [
        {
            "kind": "Method",
            "file": "renamed/App.java",
            "name": "run()",
            "semantic_id": "method:com.example.App#run()",
            "fqid": "method:com.example.App#run()@renamed/App.java",
            "semantic_hash": "abc123",
            "hash": "abc123",
            "line_start": 99,
            "line_end": 120,
        }
    ]

    added, removed, modified = branch_diff._diff_semantic_records(base, head)

    assert added == []
    assert removed == []
    assert modified == []


def test_compare_branches_orders_changed_files_first_and_fails_soft(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(branch_diff.subprocess, "run", fake_run)
    monkeypatch.setattr(
        branch_diff,
        "_git_changed_files",
        lambda *args, **kwargs: [
            {"status": "M", "base": "src/First.java", "head": "src/First.java"},
            {"status": "A", "head": "src/Added.java"},
            {"status": "M", "base": "src/Broken.java", "head": "src/Broken.java"},
        ],
    )

    def fake_manifest(repo_path: str, rel_path: str):
        calls.append((repo_path.rsplit("/", 1)[-1], rel_path))
        if rel_path == "src/First.java":
            return (
                [
                    {
                        "kind": "Method",
                        "file": rel_path,
                        "name": "run()",
                        "semantic_id": "method:com.example.First#run()",
                        "fqid": f"method:com.example.First#run()@{rel_path}",
                        "semantic_hash": "same",
                    }
                ],
                [],
            )
        if rel_path == "src/Added.java":
            return (
                [
                    {
                        "kind": "Method",
                        "file": rel_path,
                        "name": "added()",
                        "semantic_id": "method:com.example.Added#added()",
                        "fqid": f"method:com.example.Added#added()@{rel_path}",
                        "semantic_hash": "new",
                    }
                ],
                [],
            )
        if rel_path == "src/Broken.java":
            if repo_path.rsplit("/", 1)[-1] == "base":
                return ([], [f"{rel_path}: parse failure"])
            return ([], [])
        raise AssertionError(f"unexpected manifest request: {repo_path} {rel_path}")

    monkeypatch.setattr(branch_diff, "_manifest_for_file", fake_manifest)

    result = branch_diff.compare_branches("/repo", "base", "head")

    assert calls == [
        ("base", "src/First.java"),
        ("head", "src/First.java"),
        ("head", "src/Added.java"),
        ("base", "src/Broken.java"),
        ("head", "src/Broken.java"),
    ]
    assert result["modified"] == []
    assert [item["file"] for item in result["added"]] == ["src/Added.java"]
    assert result["warnings"] == ["src/Broken.java: parse failure"]
