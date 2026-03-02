from pathlib import Path

import pytest

pytest.importorskip("kuzu")
pytest.importorskip("tree_sitter_java")

from codespine.db.store import GraphStore
from codespine.indexer.engine import JavaIndexer
from codespine.search.hybrid import hybrid_search


def test_index_and_hybrid_search():
    fixture = Path(__file__).parent / "fixtures" / "java_simple"
    store = GraphStore(read_only=False)
    result = JavaIndexer(store).index_project(str(fixture), full=True)
    assert result.files_indexed >= 2

    results = hybrid_search(store, "process payment", k=5)
    assert results
    assert any("processPayment" in (r.get("fqname") or "") for r in results)


def test_incremental_no_change_reindexes_zero_files():
    fixture = Path(__file__).parent / "fixtures" / "java_simple"
    store = GraphStore(read_only=False)
    indexer = JavaIndexer(store)

    first = indexer.index_project(str(fixture), full=True)
    second = indexer.index_project(str(fixture), full=False)

    assert first.files_found >= 2
    assert second.files_found == first.files_found
    assert second.files_indexed == 0
