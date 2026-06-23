"""
Protocol for CodeSpine graph stores.

``StoreProtocol`` captures the common duck-typed interface shared by the
DuckDB (``DuckDBStore``) and Kùzu (``GraphStore``) backends so that
analysis and CLI code can type-annotate against a single abstraction.

Usage
-----
    store: StoreProtocol = DuckDBStore(...)
    assert isinstance(store, StoreProtocol)  # True (runtime_checkable)
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class StoreProtocol(Protocol):
    """Minimal interface that both ``DuckDBStore`` and ``GraphStore`` satisfy."""

    read_only: bool

    # ── Connection / lifecycle ──────────────────────────────────────────────

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        ...

    @contextmanager
    def transaction(self) -> Iterator[None]:
        ...

    def _recycle_conn(self) -> None:
        ...

    @staticmethod
    def stable_id(*parts: str) -> str:
        ...

    # ── Project ─────────────────────────────────────────────────────────────

    def upsert_project(self, project_id: str, path: str) -> None:
        ...

    def set_project_overlay_dirty(self, project_id: str, dirty: bool) -> None:
        ...

    def set_project_indexed_commit(self, project_id: str, commit: str) -> None:
        ...

    def get_project_metadata(self, project_id: str) -> dict[str, Any] | None:
        ...

    def list_project_metadata(self) -> list[dict[str, Any]]:
        ...

    def project_file_hashes(self, project_id: str) -> dict[str, dict[str, str]]:
        ...

    def project_has_embeddings(self, project_id: str) -> bool:
        ...

    def clear_project(self, project_id: str) -> None:
        ...

    # ── Files ───────────────────────────────────────────────────────────────

    def clear_file(self, file_id: str) -> None:
        ...

    def clear_files_batch(self, file_ids: list[str]) -> None:
        ...

    def clear_file_by_path(self, project_id: str, project_path: str, file_path: str) -> None:
        ...

    def upsert_file(
        self, file_id: str, path: str, project_id: str, is_test: bool, digest: str
    ) -> None:
        ...

    def upsert_files_batch(
        self, records: list[dict[str, Any]], create_mode: bool = False
    ) -> None:
        ...

    def upsert_file_from_entry(self, entry: dict, project_path: str) -> None:
        ...

    # ── Classes ─────────────────────────────────────────────────────────────

    def upsert_class(
        self,
        class_id: str,
        fqcn: str,
        name: str,
        package: str,
        file_id: str,
    ) -> None:
        ...

    def upsert_classes_batch(
        self, records: list[dict[str, Any]], create_mode: bool = False
    ) -> None:
        ...

    # ── Methods ─────────────────────────────────────────────────────────────

    def upsert_methods_batch(
        self, records: list[dict[str, Any]], create_mode: bool = False
    ) -> None:
        ...

    def list_methods(self) -> list[dict[str, Any]]:
        ...

    # ── Symbols ─────────────────────────────────────────────────────────────

    def upsert_symbols_batch(
        self, records: list[dict[str, Any]], create_mode: bool = False
    ) -> None:
        ...

    # ── Calls ───────────────────────────────────────────────────────────────

    def add_call(
        self, source_id: str, target_id: str, confidence: float, reason: str
    ) -> None:
        ...

    def add_calls_batch(
        self, records: list[dict[str, Any]], create_mode: bool = False
    ) -> None:
        ...

    # ── References ──────────────────────────────────────────────────────────

    def add_reference(
        self,
        rel: str,
        src_label: str,
        src_id: str,
        dst_label: str,
        dst_id: str,
        confidence: float,
    ) -> None:
        ...

    def add_references_batch(
        self, records: list[dict[str, Any]], create_mode: bool = False
    ) -> None:
        ...

    # ── Injections / DI ─────────────────────────────────────────────────────

    def add_injection(
        self,
        src_class_id: str,
        dst_class_id: str,
        framework: str,
        binding_type: str,
        confidence: float,
    ) -> None:
        ...

    def add_injections_batch(self, records: list[dict[str, Any]]) -> None:
        ...

    # ── Interface bindings ──────────────────────────────────────────────────

    def add_interface_binding(
        self,
        src_class_id: str,
        dst_class_id: str,
        confidence: float,
        reason: str,
    ) -> None:
        ...

    def add_interface_bindings_batch(self, records: list[dict[str, Any]]) -> None:
        ...

    # ── Communities / Flows / Coupling ──────────────────────────────────────

    def clear_communities(self) -> None:
        ...

    def clear_flows(self) -> None:
        ...

    def clear_coupling(self) -> None:
        ...

    def clear_analysis_artifacts(self) -> None:
        ...

    def set_community(
        self,
        community_id: str,
        label: str,
        cohesion: float,
        symbol_ids: list[str],
    ) -> None:
        ...

    def set_flow(
        self,
        flow_id: str,
        entry_symbol_id: str,
        kind: str,
        symbols_at_depth: list[tuple[str, int]],
    ) -> None:
        ...

    def upsert_coupling(
        self, file_a: str, file_b: str, strength: float, cochanges: int, days: int
    ) -> None:
        ...

    # ── Query / search ──────────────────────────────────────────────────────

    def query_records(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        ...

    # ── Snapshot / durability ───────────────────────────────────────────────

    def _do_snapshot(self) -> None:
        ...

    def snapshot_to_read_replica(self, background: bool = False) -> bool:
        ...

    # ── Reset / rebuild ─────────────────────────────────────────────────────

    def force_delete_all_data(self) -> list[str]:
        ...

    def rebuild_empty_db(self) -> None:
        ...
