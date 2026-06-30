from __future__ import annotations

import asyncio
import json
import types

from codespine.analysis.impact import analyze_impact
from codespine.analysis.crossmodule import link_dependency_imports
from codespine.indexer.engine import JavaIndexer
from codespine.indexer.symbol_builder import class_id, file_id, symbol_id
from codespine.mcp._ui_html import UI_HTML
from codespine.mcp.server import build_mcp_server
from codespine.sharding.store import ShardedGraphStore


def test_refresh_project_dependency_metadata_links_modules(monkeypatch, tmp_path):
    import codespine.cli as cli
    import codespine.project_state as project_state

    meta_dir = tmp_path / "meta"
    overlay_dir = tmp_path / "overlay"
    settings = types.SimpleNamespace(index_meta_dir=str(meta_dir), overlay_dir=str(overlay_dir))
    monkeypatch.setattr(cli, "SETTINGS", settings)
    monkeypatch.setattr(project_state, "SETTINGS", settings)

    root = tmp_path / "repo"
    a = root / "module-a"
    b = root / "module-b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "pom.xml").write_text(
        """
        <project>
          <modelVersion>4.0.0</modelVersion>
          <parent>
            <groupId>com.acme</groupId>
            <artifactId>root</artifactId>
            <version>1.0.0</version>
          </parent>
          <artifactId>module-a</artifactId>
          <dependencies>
            <dependency>
              <groupId>com.acme</groupId>
              <artifactId>module-b</artifactId>
            </dependency>
          </dependencies>
        </project>
        """,
        encoding="utf-8",
    )
    (b / "pom.xml").write_text(
        """
        <project>
          <modelVersion>4.0.0</modelVersion>
          <parent>
            <groupId>com.acme</groupId>
            <artifactId>root</artifactId>
            <version>1.0.0</version>
          </parent>
          <artifactId>module-b</artifactId>
        </project>
        """,
        encoding="utf-8",
    )

    cli._refresh_project_dependency_metadata([(str(a), "repo::module-a"), (str(b), "repo::module-b")])

    state_a = project_state.load_project_state("repo::module-a")
    state_b = project_state.load_project_state("repo::module-b")
    assert state_a["maven_coord"] == "com.acme:module-a"
    assert state_b["maven_coord"] == "com.acme:module-b"
    assert state_a["dependency_project_ids"] == ["repo::module-b"]


class _DependencyStore:
    def query_records(self, query: str, params: dict | None = None) -> list[dict]:
        if "s.kind = 'field'" in query:
            return []
        if "REFERENCES_TYPE" in query:
            return [
                {
                    "src": "sym-app", "dst": "sym-lib",
                    "src_name": "App", "dst_name": "TargetService",
                    "src_fqname": "com.acme.app.App", "dst_fqname": "com.acme.lib.TargetService",
                    "src_file_path": "/repo/app/App.java", "dst_file_path": "/repo/lib/TargetService.java",
                    "src_project_id": "app", "dst_project_id": "lib",
                    "confidence": 0.95,
                },
            ]
        if "MATCH (s:Symbol), (f:File)" in query:
            return [
                {
                    "id": "sym-lib",
                    "kind": "class",
                    "name": "TargetService",
                    "fqname": "com.acme.lib.TargetService",
                    "file_id": "f-lib",
                    "project_id": "lib",
                    "file_path": "/repo/lib/TargetService.java",
                    "is_test": False,
                },
                {
                    "id": "sym-other",
                    "kind": "class",
                    "name": "TargetService",
                    "fqname": "com.acme.other.TargetService",
                    "file_id": "f-other",
                    "project_id": "other",
                    "file_path": "/repo/other/TargetService.java",
                    "is_test": False,
                },
            ]
        if "MATCH (c:Class), (f:File)" in query:
            return [
                {
                    "id": "c-lib",
                    "name": "TargetService",
                    "fqcn": "com.acme.lib.TargetService",
                    "package": "com.acme.lib",
                    "file_id": "f-lib",
                    "project_id": "lib",
                    "file_path": "/repo/lib/TargetService.java",
                },
                {
                    "id": "c-other",
                    "name": "TargetService",
                    "fqcn": "com.acme.other.TargetService",
                    "package": "com.acme.other",
                    "file_id": "f-other",
                    "project_id": "other",
                    "file_path": "/repo/other/TargetService.java",
                },
            ]
        if "MATCH (m:Method), (c:Class), (f:File)" in query:
            return [
                {
                    "id": "m-lib",
                    "class_id": "c-lib",
                    "class_fqcn": "com.acme.lib.TargetService",
                    "name": "run",
                    "signature": "run()",
                    "return_type": "void",
                    "is_constructor": False,
                    "is_test": False,
                    "file_id": "f-lib",
                    "project_id": "lib",
                    "file_path": "/repo/lib/TargetService.java",
                }
            ]
        if "MATCH (p:Project)" in query:
            return []
        if "MATCH (a:Method)-[r:CALLS]->(b:Method)" in query:
            return []
        if "INJECTS" in query or "BINDS_INTERFACE" in query:
            return []
        return []


def test_find_symbol_scopes_to_dependency_projects(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])
    project_state.update_project_state("other", path="/repo/other", dependency_project_ids=[])

    async def _run():
        mcp = build_mcp_server(_DependencyStore(), lambda: ".")
        result = await mcp.call_tool("find_symbol", {"name": "TargetService", "project": "app"})
        payload = json.loads(result.content[0].text)
        assert payload["available"] is True
        assert sorted(payload["by_project"].keys()) == ["lib"]

    asyncio.run(_run())


def test_analyze_impact_includes_dependent_projects(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    payload = analyze_impact(_DependencyStore(), "TargetService", project="app")
    assert payload["dependent_projects"] == ["app"]


def test_analyze_impact_reports_importing_classes(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    class _Store(_DependencyStore):
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (src:Symbol)-[r:REFERENCES_TYPE]->(dst:Symbol)" in query:
                return [{
                    "src": "sym-app",
                    "dst": "sym-lib",
                    "src_name": "App",
                    "src_fqname": "com.acme.app.App",
                    "dst_name": "TargetService",
                    "dst_fqname": "com.acme.lib.TargetService",
                    "src_file_path": "/repo/app/App.java",
                    "dst_file_path": "/repo/lib/TargetService.java",
                    "src_project_id": "app",
                    "dst_project_id": "lib",
                    "confidence": 0.95,
                    "rel": "REFERENCES_TYPE",
                }]
            return super().query_records(query, params)

    payload = analyze_impact(_Store(), "TargetService", project="app")
    assert payload["importing_classes"][0]["project_id"] == "app"


def test_explain_reports_cross_project_importers(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    class _ExplainStore(_DependencyStore):
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (src:Symbol)-[r:REFERENCES_TYPE]->(dst:Symbol)" in query:
                return [{
                    "src": "sym-app",
                    "dst": "sym-lib",
                    "src_name": "App",
                    "src_fqname": "com.acme.app.App",
                    "dst_name": "TargetService",
                    "dst_fqname": "com.acme.lib.TargetService",
                    "src_file_path": "/repo/app/App.java",
                    "dst_file_path": "/repo/lib/TargetService.java",
                    "src_project_id": "app",
                    "dst_project_id": "lib",
                    "confidence": 0.95,
                    "rel": "REFERENCES_TYPE",
                }]
            if "MATCH (m:Method)-[:CALLS]->(callee:Method)" in query or "MATCH (caller:Method)-[:CALLS]->(m:Method)" in query:
                return []
            if "MATCH (s:Symbol {id: $sid})-[:IN_COMMUNITY]->(c:Community)" in query:
                return []
            return super().query_records(query, params)

    async def _run():
        mcp = build_mcp_server(_ExplainStore(), lambda: ".")
        result = await mcp.call_tool("explain", {"symbol": "TargetService", "project": "app"})
        payload = json.loads(result.content[0].text)
        assert payload["imported_by"][0]["project_id"] == "app"

    asyncio.run(_run())


def test_rename_plan_includes_import_reference_sites(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    class _RenameStore(_DependencyStore):
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (src:Symbol)-[r:REFERENCES_TYPE]->(dst:Symbol)" in query:
                return [{
                    "src": "sym-app",
                    "dst": "c-lib",
                    "src_name": "App",
                    "src_fqname": "com.acme.app.App",
                    "dst_name": "TargetService",
                    "dst_fqname": "com.acme.lib.TargetService",
                    "src_file_path": "/repo/app/App.java",
                    "dst_file_path": "/repo/lib/TargetService.java",
                    "src_project_id": "app",
                    "dst_project_id": "lib",
                    "confidence": 0.95,
                    "rel": "REFERENCES_TYPE",
                }]
            if "MATCH (caller:Method)-[:CALLS]->(m:Method)" in query or "MATCH (child:Method)-[:OVERRIDES]->(m:Method" in query:
                return []
            return super().query_records(query, params)

    async def _run():
        mcp = build_mcp_server(_RenameStore(), lambda: ".")
        result = await mcp.call_tool("rename_plan", {"symbol": "TargetService", "new_name": "RenamedService", "project": "lib"})
        payload = json.loads(result.content[0].text)
        app_changes = [f for f in payload["files_to_modify"] if f.get("file_path") == "/repo/app/App.java"]
        assert app_changes
        assert any(change.get("kind") == "import_reference" for change in app_changes[0]["changes"])

    asyncio.run(_run())


def test_file_context_reports_importers(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)

    class _FileStore(_DependencyStore):
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (f:File) WHERE f.path = $path" in query:
                return [{"id": "f-lib", "pid": "lib"}]
            if "MATCH (s:Symbol) WHERE s.file_id = $fid RETURN s.kind as kind" in query:
                return [{"kind": "class", "name": "TargetService", "line": 1}]
            if "MATCH (s:Symbol) WHERE s.file_id = $fid AND s.kind = 'class'" in query:
                return [{"id": "c-lib"}]
            if "MATCH (caller:Method)-[:CALLS]->(m:Method)" in query:
                return []
            if "MATCH (s:Symbol)-[:IN_COMMUNITY]->(c:Community)" in query:
                return []
            if "CO_CHANGED_WITH" in query:
                return []
            if "MATCH (src:Symbol)-[r:REFERENCES_TYPE]->(dst:Symbol)" in query:
                return [{
                    "src": "sym-app",
                    "dst": "c-lib",
                    "src_name": "App",
                    "src_fqname": "com.acme.app.App",
                    "dst_name": "TargetService",
                    "dst_fqname": "com.acme.lib.TargetService",
                    "src_file_path": "/repo/app/App.java",
                    "dst_file_path": "/repo/lib/TargetService.java",
                    "src_project_id": "app",
                    "dst_project_id": "lib",
                    "confidence": 0.95,
                    "rel": "REFERENCES_TYPE",
                }]
            return super().query_records(query, params)

    async def _run():
        mcp = build_mcp_server(_FileStore(), lambda: ".")
        result = await mcp.call_tool("file_context", {"file_path": "/repo/lib/TargetService.java"})
        payload = json.loads(result.content[0].text)
        assert payload["imported_by"][0]["project_id"] == "app"

    asyncio.run(_run())


def test_get_dependency_graph_tool(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    class _GraphStore(_DependencyStore):
        def list_project_dependencies(self, project_id: str, reverse: bool = False):
            if reverse:
                return ["app"] if project_id == "lib" else []
            return ["lib"] if project_id == "app" else []

    async def _run():
        mcp = build_mcp_server(_GraphStore(), lambda: ".")
        result = await mcp.call_tool("get_dependency_graph", {"project": "app"})
        payload = json.loads(result.content[0].text)
        assert sorted(node["project_id"] for node in payload["nodes"]) == ["app", "lib"]
        assert len(payload["edges"]) == 1
        assert payload["edges"][0]["src"] == "app"
        assert payload["edges"][0]["dst"] == "lib"

    asyncio.run(_run())


def test_find_project_usages_tool(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    class _UsageStore(_DependencyStore):
        def query_records(self, query: str, params: dict | None = None) -> list[dict]:
            if "MATCH (src:Symbol)-[r:REFERENCES_TYPE]->(dst:Symbol)" in query:
                return [{
                    "src": "sym-app",
                    "dst": "c-lib",
                    "src_name": "App",
                    "src_fqname": "com.acme.app.App",
                    "dst_name": "TargetService",
                    "dst_fqname": "com.acme.lib.TargetService",
                    "src_file_path": "/repo/app/App.java",
                    "dst_file_path": "/repo/lib/TargetService.java",
                    "src_project_id": "app",
                    "dst_project_id": "lib",
                    "confidence": 0.95,
                    "rel": "REFERENCES_TYPE",
                }]
            return super().query_records(query, params)

    async def _run():
        mcp = build_mcp_server(_UsageStore(), lambda: ".")
        result = await mcp.call_tool("find_project_usages", {"project": "lib"})
        payload = json.loads(result.content[0].text)
        assert payload["dependent_projects"] == ["app"]
        assert "app" in payload["imports_by_project"]

    asyncio.run(_run())


def test_ui_html_exposes_dependency_actions_and_endpoints():
    assert "showProjectDependencies" in UI_HTML
    assert "showProjectUsages" in UI_HTML
    assert "/api/dependency-graph" in UI_HTML
    assert "/api/project-usages" in UI_HTML


def test_duckdb_persists_project_dependencies(tmp_path):
    from codespine.db.duckdb_store import DuckDBStore

    store = DuckDBStore(read_only=False, db_path_override=str(tmp_path / "db"), snapshot_path_override=str(tmp_path / "db_read"))
    store.upsert_project("app", "/repo/app")
    store.upsert_project("lib", "/repo/lib")
    store.upsert_project_dependencies("app", ["lib"])

    assert store.list_project_dependencies("app") == ["lib"]
    assert store.list_project_dependencies("lib", reverse=True) == ["app"]


def test_link_dependency_imports_creates_reference_edges(monkeypatch, tmp_path):
    import codespine.project_state as project_state

    settings = types.SimpleNamespace(index_meta_dir=str(tmp_path / "meta"), overlay_dir=str(tmp_path / "overlay"))
    monkeypatch.setattr(project_state, "SETTINGS", settings)
    monkeypatch.setattr("codespine.indexer.engine.SETTINGS", settings)

    sg = ShardedGraphStore(read_only=False, num_shards=1, shards_dir=str(tmp_path / "shards"), backend="duckdb")
    app_store = sg.shard("app")
    lib_store = sg.shard("lib")
    app_store.upsert_project("app", "/repo/app")
    lib_store.upsert_project("lib", "/repo/lib")
    app_store.upsert_project_dependencies("app", ["lib"])
    project_state.update_project_state("app", path="/repo/app", dependency_project_ids=["lib"])
    project_state.update_project_state("lib", path="/repo/lib", dependency_project_ids=[])

    app_file = file_id("app", "src/main/java/com/acme/app/App.java")
    lib_file = file_id("lib", "src/main/java/com/acme/lib/TargetService.java")
    app_store.upsert_files_batch([{"id": app_file, "path": "/repo/app/src/main/java/com/acme/app/App.java", "project_id": "app", "is_test": False, "hash": "a"}])
    lib_store.upsert_files_batch([{"id": lib_file, "path": "/repo/lib/src/main/java/com/acme/lib/TargetService.java", "project_id": "lib", "is_test": False, "hash": "b"}])

    app_class_id = class_id("com.acme.app.App", "app")
    lib_class_id = class_id("com.acme.lib.TargetService", "lib")
    app_store.upsert_classes_batch([{"id": app_class_id, "fqcn": "com.acme.app.App", "name": "App", "package": "com.acme.app", "file_id": app_file}])
    lib_store.upsert_classes_batch([{"id": lib_class_id, "fqcn": "com.acme.lib.TargetService", "name": "TargetService", "package": "com.acme.lib", "file_id": lib_file}])

    app_store.upsert_symbols_batch([{"id": symbol_id("class", "com.acme.app.App", "app"), "kind": "class", "name": "App", "fqname": "com.acme.app.App", "file_id": app_file, "line": 1, "col": 1, "embedding": None}])
    lib_store.upsert_symbols_batch([{"id": symbol_id("class", "com.acme.lib.TargetService", "lib"), "kind": "class", "name": "TargetService", "fqname": "com.acme.lib.TargetService", "file_id": lib_file, "line": 1, "col": 1, "embedding": None}])

    meta_path = JavaIndexer._meta_cache_path("app")
    meta_path.parent.mkdir(parents=True, exist_ok=True) if hasattr(meta_path, 'parent') else None
    import os
    os.makedirs(settings.index_meta_dir, exist_ok=True)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump({app_file: {"imports": ["com.acme.lib.TargetService"]}}, fh)

    created = link_dependency_imports(sg, project_ids=["app"])
    rows = app_store.query_records(
        "SELECT count(*) as n FROM references_type WHERE src_id = ? AND dst_id = ?",
        [symbol_id("class", "com.acme.app.App", "app"), symbol_id("class", "com.acme.lib.TargetService", "lib")],
    )

    assert created == 1
    assert int(rows[0]["n"]) == 1
