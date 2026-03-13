from __future__ import annotations

import pytest

pytest.importorskip("kuzu")

from codespine.db.store import GraphStore


def test_open_with_recovery_rebuilds_legacy_db(monkeypatch):
    opened: list[str] = []
    removed: list[str] = []
    calls = {"count": 0}

    def fake_open(self, path: str):
        calls["count"] += 1
        opened.append(path)
        if calls["count"] == 1:
            raise RuntimeError("Storage version mismatch: unsupported database")
        return object()

    monkeypatch.setattr(GraphStore, "_open_db", fake_open)
    monkeypatch.setattr(GraphStore, "_remove_db_path", staticmethod(lambda path: removed.append(path)))

    store = GraphStore.__new__(GraphStore)
    store.read_only = False
    store._tls = None

    db = GraphStore._open_with_recovery(store, "/tmp/test-codespine-db")

    assert db is not None
    assert opened == ["/tmp/test-codespine-db", "/tmp/test-codespine-db"]
    assert removed == ["/tmp/test-codespine-db"]


def test_open_with_recovery_does_not_remove_on_permission_error(monkeypatch):
    removed: list[str] = []

    def fake_open(self, path: str):
        raise RuntimeError("Operation not permitted")

    monkeypatch.setattr(GraphStore, "_open_db", fake_open)
    monkeypatch.setattr(GraphStore, "_remove_db_path", staticmethod(lambda path: removed.append(path)))

    store = GraphStore.__new__(GraphStore)
    store.read_only = False
    store._tls = None

    with pytest.raises(RuntimeError, match="Operation not permitted"):
        GraphStore._open_with_recovery(store, "/tmp/test-codespine-db")

    assert removed == []
