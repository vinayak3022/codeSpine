import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # Legacy single-DB paths — kept for backward compat and as defaults when
    # sharding is disabled (num_shards == 1 or CODESPINE_SHARDS not set).
    db_path: str = os.path.expanduser("~/.codespine_db")
    db_snapshot_path: str = os.path.expanduser("~/.codespine_db_read")

    # Sharding — new layout stores each shard under shards_dir/{N}/db
    # num_shards: int, overridable via CODESPINE_SHARDS env var at runtime.
    # ShardRouter reads CODESPINE_SHARDS directly; this field is the compiled default.
    num_shards: int = 4
    shards_dir: str = os.path.expanduser("~/.codespine/shards")

    # Storage backend: "kuzu" (default, property-graph) or "duckdb" (relational).
    # Override at runtime via CODESPINE_BACKEND env var before starting the process.
    backend: str = field(default_factory=lambda: os.environ.get("CODESPINE_BACKEND", "duckdb"))

    pid_file: str = os.path.expanduser("~/.codespine.pid")
    log_file: str = os.path.expanduser("~/.codespine.log")
    task_registry_path: str = os.path.expanduser("~/.codespine_tasks.json")
    embedding_cache_path: str = os.path.expanduser("~/.codespine_embedding_cache.json")
    index_meta_dir: str = os.path.expanduser("~/.codespine_index_meta")
    overlay_dir: str = os.path.expanduser("~/.codespine_overlay")
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    vector_dim: int = 768
    cross_encoder_model: str = ""
    rrf_k: int = 60
    semantic_candidate_pool: int = 5000
    write_batch_size: int = 500
    index_file_batch_size: int = 1000
    index_method_batch_size: int = 10000
    index_symbol_batch_size: int = 10000
    edge_write_batch_size: int = 5000
    default_coupling_days: int = 5
    default_min_coupling_strength: float = 0.3
    default_min_cochanges: int = 3
    default_global_interval_s: int = 30
    default_overlay_debounce_ms: int = 1500

    # Query-time candidate pool for semantic search (0 = use SQL-native pushdown limit)
    # Can be set per-query via CODESPINE_CANDIDATE_POOL env var or the pool_size param.
    query_candidate_pool: int = 5000

    # Health-check ping interval in seconds for the supervisor process (0 = disabled).
    supervisor_health_interval_s: int = 10

    # Max consecutive failed health pings before restart (0 = disabled).
    supervisor_health_max_failures: int = 3

    # Max restart attempts before the supervisor enters degraded mode and exits
    # (0 = unlimited, default 12 ≈ ~10 minutes at 1-2-4-…-30s backoff window).
    supervisor_max_restarts: int = 12

    # Background MCP daemon transport / bind address.
    mcp_http_host: str = "127.0.0.1"
    mcp_http_port: int = 8766

    # Whether to attempt automatic DB repair on corruption at startup.
    auto_repair_on_startup: bool = True


SETTINGS = Settings()
