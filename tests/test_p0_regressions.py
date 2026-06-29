from __future__ import annotations

import asyncio
import inspect
import json
import time
import types
from pathlib import Path

from codespine.indexer.engine import JavaIndexer
from codespine.mcp.server import build_mcp_server
from codespine.overlay.merge import merged_class_records
from codespine.sharding.store import ShardedGraphStore


class _OverlaylessStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "MATCH (c:Class), (f:File)" in query:
            return [
                {
                    "id": "c1",
                    "name": "TransactionService",
                    "fqcn": "com.example.TransactionService",
                    "package": "com.example",
                    "file_id": "f1",
                    "project_id": "app",
                    "file_path": "/tmp/TransactionService.java",
                }
            ]
        if "MATCH (m:Method), (c:Class), (f:File)" in query:
            return []
        if "s.kind = 'field'" in query:
            return []
        if "MATCH (p:Project)" in query:
            return []
        return []


def test_merged_class_records_handles_missing_overlay_store() -> None:
    rows = merged_class_records(_OverlaylessStore(), None)
    assert len(rows) == 1
    assert rows[0]["name"] == "TransactionService"


def test_sharded_store_overlay_store_available_in_empty_read_only_mode(tmp_path: Path) -> None:
    sg = ShardedGraphStore(read_only=True, num_shards=1, shards_dir=str(tmp_path / "shards"), backend="duckdb")
    overlay_store = sg.overlay_store
    assert overlay_store is not None
    assert hasattr(overlay_store, "list_projects")


def test_detect_projects_in_workspace_promotes_module_path_to_reactor_root(tmp_path: Path) -> None:
    root = tmp_path / "vision"
    module = root / "vision-server"
    module.mkdir(parents=True)
    (root / "pom.xml").write_text("<project><modules><module>vision-server</module></modules></project>", encoding="utf-8")
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")

    projects = JavaIndexer.detect_projects_in_workspace(str(module))
    assert projects == [str(root.resolve())]


def test_supervisor_uses_http_transport_for_background_daemon() -> None:
    import codespine.cli as cli

    source = inspect.getsource(cli.main.commands["supervise-mcp"].callback)
    assert "streamable-http" in source
    assert "--transport" in source


def test_find_symbol_handles_missing_overlay_store_without_none_regression() -> None:
    async def _run():
        mcp = build_mcp_server(_OverlaylessStore(), lambda: ".")
        result = await mcp.call_tool("find_symbol", {"name": "TransactionService"})
        payload = json.loads(result.content[0].text)
        assert payload["available"] is True
        assert payload["by_project"]["app"]["classes"][0]["name"] == "TransactionService"

    asyncio.run(_run())


def test_cleanup_duplicate_projects_uses_clear_project_not_cypher_delete(monkeypatch):
    import codespine.cli as cli

    class _Overlay:
        def clear_project(self, project_id):
            cleared.append(("overlay", project_id))

    class _Store:
        overlay_store = _Overlay()

        def query_records(self, query, params=None):
            if "count(f)" in query:
                return [{"c": 1 if params and params.get("pid") == "keep" else 0}]
            raise AssertionError(f"unexpected query: {query}")

        def clear_project(self, project_id):
            cleared.append(("store", project_id))

        def snapshot_to_read_replica(self):
            cleared.append(("snapshot", None))

    cleared = []
    monkeypatch.setattr(
        cli,
        "list_project_states",
        lambda: [
            {"project_id": "keep", "path": "/repo/app"},
            {"project_id": "dup", "path": "/repo/app"},
        ],
    )
    monkeypatch.setattr("codespine.project_state.delete_project_state", lambda pid: cleared.append(("state", pid)))

    removed = cli._cleanup_duplicate_projects(_Store())

    assert removed == 1
    assert ("store", "dup") in cleared
    assert ("state", "dup") in cleared
    assert ("overlay", "dup") in cleared
    assert ("snapshot", None) in cleared


def test_index_project_allows_valid_java_files_with_no_classes(monkeypatch, tmp_path):
    from codespine.db.duckdb_store import DuckDBStore
    from codespine.indexer.engine import JavaIndexer

    root = tmp_path / "app"
    root.mkdir()
    java_file = root / "package-info.java"
    java_file.write_text("@Deprecated\npackage example;\n", encoding="utf-8")

    class _Parsed:
        package = "example"
        imports = []
        classes = []

    monkeypatch.setattr("codespine.indexer.engine.parse_java_source", lambda _src: _Parsed())

    store = DuckDBStore(read_only=False, db_path_override=str(tmp_path / "db"), snapshot_path_override=str(tmp_path / "db_read"))
    indexer = JavaIndexer(store)

    result = indexer.index_project(str(root), full=True, embed=False)
    assert result.files_found == 1
    assert result.classes_indexed == 0
    assert result.methods_indexed == 0



def test_status_reports_sharded_storage(monkeypatch):
    import json as _json
    import types
    from click.testing import CliRunner
    import codespine.cli as cli

    monkeypatch.setattr(cli, '_is_running', lambda: True)
    monkeypatch.setattr(cli, '_open_store', lambda read_only=True: object())
    monkeypatch.setattr(cli, 'get_overlay_status', lambda store: [])
    monkeypatch.setattr(cli, 'active_tasks', lambda limit=10: [])
    monkeypatch.setattr(cli, '_project_summaries', lambda: [])
    monkeypatch.setattr(cli, '_read_runtime_state', lambda: {})
    monkeypatch.setattr(cli, 'index_health', lambda store: {'summary': {}})
    monkeypatch.setattr(cli, '_count_codespine_processes', lambda _: 0)
    monkeypatch.setattr(cli, '_shard_storage_paths', lambda kind='db': ['/tmp/shards/0/db'] if kind == 'db' else ['/tmp/shards/0/db_read'])
    monkeypatch.setattr(cli, '_shard_storage_bytes', lambda kind='db': 123 if kind == 'db' else 45)
    monkeypatch.setattr(cli, 'SETTINGS', types.SimpleNamespace(
        pid_file='/tmp/pid', log_file='/tmp/log', shards_dir='/tmp/shards', overlay_dir='/tmp/overlay',
        mcp_http_host='127.0.0.1', mcp_http_port=8766,
    ))

    result = CliRunner().invoke(cli.main, ['status', '--json'])
    assert result.exit_code == 0
    payload = _json.loads(result.output)
    assert payload['db_path'] == '/tmp/shards'
    assert payload['db_paths'] == ['/tmp/shards/0/db']
    assert payload['db_size_bytes'] == 123
    assert payload['read_replica'] == '/tmp/shards'
    assert payload['read_replica_paths'] == ['/tmp/shards/0/db_read']
    assert payload['read_replica_size_bytes'] == 45



def test_clean_removes_shards_dir(monkeypatch, tmp_path):
    import types
    from click.testing import CliRunner
    import codespine.cli as cli

    shards = tmp_path / 'shards'
    shards.mkdir()
    (shards / '0').mkdir()
    (shards / '0' / 'db').write_text('x', encoding='utf-8')
    snap = tmp_path / 'db_read'
    snap.write_text('x', encoding='utf-8')
    overlay = tmp_path / 'overlay'
    overlay.mkdir()

    monkeypatch.setattr(cli, 'SETTINGS', types.SimpleNamespace(
        pid_file=str(tmp_path / 'pid'),
        log_file=str(tmp_path / 'log'),
        db_path=str(tmp_path / 'legacy_db'),
        db_snapshot_path=str(snap),
        shards_dir=str(shards),
        overlay_dir=str(overlay),
        index_meta_dir=str(tmp_path / 'meta'),
    ))

    result = CliRunner().invoke(cli.main, ['clean', '--force'])
    assert result.exit_code == 0
    assert not shards.exists()
    assert not snap.exists()
    assert not overlay.exists()



def test_plan_incremental_forces_reindex_when_db_empty_but_meta_cache_present(tmp_path):
    from codespine.indexer.engine import JavaIndexer

    root = tmp_path / 'app'
    root.mkdir()
    f = root / 'A.java'
    f.write_text('class A {}\n', encoding='utf-8')

    class _Store:
        pass

    indexer = JavaIndexer(_Store())
    to_reindex, deleted, meta = indexer._plan_incremental(
        'app',
        str(root),
        [str(f)],
        {},
        {'stale': {'mtime_ns': 1, 'size': 1, 'hash': 'abc'}},
    )
    assert to_reindex == [str(f)]
    assert deleted == []
    assert meta == {}



def test_start_fails_cleanly_when_mcp_port_in_use(monkeypatch):
    from click.testing import CliRunner
    import codespine.cli as cli

    monkeypatch.setattr(cli, '_is_running', lambda: False)
    monkeypatch.setattr(cli, '_cleanup_orphan_codespine_processes', lambda *args: [])
    monkeypatch.setattr(cli, '_safe_remove_pid_file', lambda: None)
    monkeypatch.setattr(cli, '_port_is_in_use', lambda host, port: True)

    result = CliRunner().invoke(cli.main, ['start'])
    assert result.exit_code == 0
    assert 'already in use' in result.output


def test_force_reset_removes_meta_overlay_and_runtime_state(monkeypatch, tmp_path):
    from click.testing import CliRunner
    import codespine.cli as cli

    overlay = tmp_path / 'overlay'
    overlay.mkdir()
    meta = tmp_path / 'meta'
    meta.mkdir()
    embedding = tmp_path / 'embed.json'
    embedding.write_text('{}', encoding='utf-8')
    runtime = tmp_path / 'runtime.json'
    runtime.write_text('{}', encoding='utf-8')
    tasks = tmp_path / 'tasks.json'
    tasks.write_text('{}', encoding='utf-8')

    monkeypatch.setattr(cli, 'SETTINGS', types.SimpleNamespace(
        overlay_dir=str(overlay),
        index_meta_dir=str(meta),
        embedding_cache_path=str(embedding),
        task_registry_path=str(tasks),
    ))
    monkeypatch.setattr(cli, '_runtime_state_path', lambda: str(runtime))

    class _Store:
        def force_delete_all_data(self):
            return []

    monkeypatch.setattr(cli, 'ShardedGraphStore', lambda read_only=False: _Store())

    result = CliRunner().invoke(cli.main, ['force-reset', '--force'])
    assert result.exit_code == 0
    assert not overlay.exists()
    assert not meta.exists()
    assert not embedding.exists()
    assert not runtime.exists()
    assert not tasks.exists()


def test_sharded_snapshot_to_read_replica_reports_failures():
    class _Shard:
        def __init__(self, ok):
            self.ok = ok

        def snapshot_to_read_replica(self, background=False):
            return self.ok

    sg = object.__new__(ShardedGraphStore)
    sg._pool = {0: _Shard(True), 1: _Shard(False)}

    assert sg.snapshot_all(background=False) == {0: True, 1: False}
    assert sg.snapshot_to_read_replica(background=False) is False


def test_list_projects_hides_state_only_stale_entries(monkeypatch):
    class _Store:
        def query_records(self, query, params=None):
            return []

    async def _run():
        import codespine.mcp.server as server

        monkeypatch.setattr(
            server,
            '_project_inventory',
            lambda _store: [{
                'project_id': 'ghost',
                'path': '/repo/ghost',
                'state_only': True,
                'snapshot_valid': False,
                'write_db_valid': False,
            }],
        )
        mcp = build_mcp_server(_Store(), lambda: '.')
        result = await mcp.call_tool('list_projects', {})
        payload = json.loads(result.content[0].text)
        assert payload['available'] is False

    asyncio.run(_run())


def test_index_project_does_not_false_timeout_queued_files(monkeypatch, tmp_path):
    from codespine.db.duckdb_store import DuckDBStore
    import codespine.indexer.engine as engine

    root = tmp_path / 'app'
    root.mkdir()
    for idx in range(3):
        (root / f'A{idx}.java').write_text('class A {}\n', encoding='utf-8')

    def _fake_parse(file_path: str, root_path: str, project_id: str) -> dict:
        time.sleep(0.02)
        rel_path = Path(file_path).name
        parsed = types.SimpleNamespace(
            package='example',
            imports=[],
            classes=[types.SimpleNamespace(
                name=f'C{rel_path}',
                package='example',
                fqcn=f'example.C{rel_path}',
                line=1,
                col=1,
                modifiers=[],
                annotations=[],
                interfaces=[],
                extends=None,
                field_types={},
                methods=[],
                fields=[],
            )],
        )
        return {
            'file_path': file_path,
            'rel_path': rel_path,
            'source': b'class A {}',
            'parsed': parsed,
            'f_id': f'{project_id}:{rel_path}',
            'digest': rel_path,
            'is_test': False,
            'scope': project_id,
        }

    monkeypatch.setattr(engine, '_parse_file_worker', _fake_parse)
    monkeypatch.setattr(engine, '_PARSE_TIMEOUT_SECS', 0.01)
    monkeypatch.setattr(engine, '_PARSE_HEARTBEAT_PERIOD', 0.005)
    monkeypatch.setattr(engine.os, 'cpu_count', lambda: 1)

    store = DuckDBStore(read_only=False, db_path_override=str(tmp_path / 'db'), snapshot_path_override=str(tmp_path / 'db_read'))
    result = JavaIndexer(store).index_project(str(root), full=True, embed=False)

    assert result.classes_indexed == 3
    assert result.skipped_files == 0
