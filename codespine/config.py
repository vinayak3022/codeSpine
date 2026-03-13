import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = os.path.expanduser("~/.codespine_db")
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
    index_file_batch_size: int = 64
    edge_write_batch_size: int = 2000
    default_coupling_months: int = 6
    default_min_coupling_strength: float = 0.3
    default_min_cochanges: int = 3
    default_global_interval_s: int = 30
    default_overlay_debounce_ms: int = 1500


SETTINGS = Settings()
