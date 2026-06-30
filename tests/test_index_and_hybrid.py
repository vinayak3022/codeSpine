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

    # Scope to the fixture project to avoid interference from other indexed projects.
    project_id = result.project_id
    results = hybrid_search(store, "process payment", k=5, project=project_id)
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


def test_index_batches_embeddings_per_file_chunk(monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "java_simple"
    store = GraphStore(read_only=False)
    indexer = JavaIndexer(store)

    batches: list[list[str]] = []

    def fake_embed_texts(texts, dim=None):
        batches.append(list(texts))
        return [[float(i)] * 768 for i, _ in enumerate(texts)]

    monkeypatch.setattr("codespine.indexer.engine.embed_texts", fake_embed_texts)

    result = indexer.index_project(str(fixture), full=True)

    assert result.files_indexed >= 2
    assert len(batches) == 1
    assert len(batches[0]) == result.classes_indexed + result.methods_indexed
