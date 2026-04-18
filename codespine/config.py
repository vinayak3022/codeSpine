import os
from dataclasses import dataclass


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

    pid_file: str = os.path.expanduser("~/.codespine.pid")
    log_file: str = os.path.expanduser("~/.codespine.log")
    embedding_cache_path: str = os.path.expanduser("~/.codespine_embedding_cache.json")
    index_meta_dir: str = os.path.expanduser("~/.codespine_index_meta")
    overlay_dir: str = os.path.expanduser("~/.codespine_overlay")
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    vector_dim: int = 384
    rrf_k: int = 60
    semantic_candidate_pool: int = 2000
    write_batch_size: int = 500
    index_file_batch_size: int = 20
    edge_write_batch_size: int = 500
    default_coupling_days: int = 5
    default_min_coupling_strength: float = 0.3
    default_min_cochanges: int = 3
    default_global_interval_s: int = 30
    default_overlay_debounce_ms: int = 1500


SETTINGS = Settings()
