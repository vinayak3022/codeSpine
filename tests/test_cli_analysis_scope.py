from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from codespine.cli import main


class _Store:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict | None]] = []

    def query_records(self, query: str, params: dict | None = None):
        self.queries.append((query, params))
        return [{"id": "c1", "label": "pkg.one", "cohesion": 0.9}]


@pytest.mark.parametrize(
    ("command", "args", "attr", "expected_key", "expected_value", "payload"),
    [
        ("context", ["context", "Foo", "--project", "app", "--json"], "build_symbol_context", "project", "app", {"query": "Foo"}),
        ("impact", ["impact", "Foo", "--project", "app", "--json"], "analyze_impact", "project", "app", {"target": "Foo"}),
        ("deadcode", ["deadcode", "--project", "app", "--json"], "detect_dead_code", "project", "app", [{"method_id": "m1"}]),
        ("flow", ["flow", "--entry", "main", "--project", "app", "--json"], "trace_execution_flows", "project", "app", [{"entry": "m1"}]),
    ],
)
def test_cli_analysis_commands_forward_project(monkeypatch, command, args, attr, expected_key, expected_value, payload):
    captured: dict[str, object] = {}
    store = object()

    def fake_open_store(read_only=True):
        captured["read_only"] = read_only
        return store

    def fake(*f_args, **f_kwargs):
        captured["args"] = f_args
        captured.update(f_kwargs)
        return payload

    monkeypatch.setattr("codespine.cli._open_store", fake_open_store)
    monkeypatch.setattr(f"codespine.cli.{attr}", fake)

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0
    assert captured["read_only"] is True
    assert captured[expected_key] == expected_value
    assert captured["args"][0] is store
    if command in {"context", "impact"}:
        assert captured["args"][1] == "Foo"
    payload_out = json.loads(result.output)
    assert payload_out


def test_cli_community_lists_project_scoped_communities_without_refresh(monkeypatch):
    captured: dict[str, object] = {}
    store = _Store()

    def fake_open_store(read_only=True):
        captured["read_only"] = read_only
        return store

    def fail_if_called(*args, **kwargs):
        raise AssertionError("community refresh should not run by default")

    monkeypatch.setattr("codespine.cli._open_store", fake_open_store)
    monkeypatch.setattr("codespine.cli.detect_communities", fail_if_called)

    result = CliRunner().invoke(main, ["community", "--project", "app", "--json"])

    assert result.exit_code == 0
    assert captured["read_only"] is True
    query, params = store.queries[0]
    assert "f.project_id = $proj" in query
    assert params == {"proj": "app"}
    assert json.loads(result.output) == [{"id": "c1", "label": "pkg.one", "cohesion": 0.9}]


def test_cli_community_refresh_rejects_project_scope(monkeypatch):
    captured: dict[str, object] = {}
    store = object()

    def fake_open_store(read_only=True):
        captured["read_only"] = read_only
        return store

    monkeypatch.setattr("codespine.cli._open_store", fake_open_store)

    result = CliRunner().invoke(main, ["community", "--refresh", "--project", "app", "--json"])

    assert result.exit_code != 0
    assert captured["read_only"] is False
    assert "Scoped community refresh is not supported" in result.output


def test_cli_coupling_forwards_project_to_read_and_compute(monkeypatch):
    captured: dict[str, object] = {}

    class _Store:
        def get_project_metadata(self, project_id: str):
            captured["metadata_project_id"] = project_id
            return {"path": "/indexed/app"}

    def fake_open_store(read_only=True):
        captured["read_only"] = read_only
        return _Store()

    def fake_compute(store_obj, repo_path: str, project_id: str, days: int = 5, min_strength: float = 0.3, min_cochanges: int = 3, progress=None):
        captured["compute"] = {"store": store_obj, "repo_path": repo_path, "project_id": project_id, "days": days, "min_strength": min_strength, "min_cochanges": min_cochanges}
        return [{"file_a": "a", "file_b": "b", "strength": 0.9, "cochanges": 5}]

    def fake_get(store_obj, symbol: str | None = None, days: int = 5, min_strength: float = 0.3, min_cochanges: int = 3, project: str | None = None):
        captured["read_project"] = project
        captured["read_args"] = {"symbol": symbol, "days": days, "min_strength": min_strength, "min_cochanges": min_cochanges}
        return {"symbol": symbol, "couplings": [{"file": "a", "coupled_file": "b"}]}

    monkeypatch.setattr("codespine.cli._open_store", fake_open_store)
    monkeypatch.setattr("codespine.cli.os.getcwd", lambda: "/cwd")
    monkeypatch.setattr("codespine.cli.compute_coupling", fake_compute)
    monkeypatch.setattr("codespine.cli.get_coupling", fake_get)

    result = CliRunner().invoke(main, ["coupling", "--project", "app", "--json"])

    assert result.exit_code == 0
    assert captured["read_only"] is False
    assert captured["metadata_project_id"] == "app"
    assert isinstance(captured["compute"]["store"], _Store)
    assert captured["compute"]["project_id"] == "app"
    assert captured["compute"]["repo_path"] == "/indexed/app"
    assert captured["read_project"] == "app"
    assert json.loads(result.output) == {"symbol": None, "couplings": [{"file": "a", "coupled_file": "b"}]}
