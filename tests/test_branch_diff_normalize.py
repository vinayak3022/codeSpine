import pytest

pytest.importorskip("tree_sitter_java")

from codespine.diff.branch_diff import _normalize_java_snippet


def test_normalize_ignores_comments_and_whitespace():
    a = """
    // comment
    public void run() {\n   call(); /* x */ }\n
    """
    b = "public   void run(){call();}"
    assert _normalize_java_snippet(a) == _normalize_java_snippet(b)
