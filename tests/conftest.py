import sys
from pathlib import Path

import pytest

from codespine.config import SETTINGS

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_codespine_paths(tmp_path: Path):
    original = {
        "db_path": SETTINGS.db_path,
        "overlay_dir": SETTINGS.overlay_dir,
        "index_meta_dir": SETTINGS.index_meta_dir,
        "embedding_cache_path": SETTINGS.embedding_cache_path,
        "pid_file": SETTINGS.pid_file,
        "log_file": SETTINGS.log_file,
    }
    object.__setattr__(SETTINGS, "db_path", str(tmp_path / "db"))
    object.__setattr__(SETTINGS, "overlay_dir", str(tmp_path / "overlay"))
    object.__setattr__(SETTINGS, "index_meta_dir", str(tmp_path / "meta"))
    object.__setattr__(SETTINGS, "embedding_cache_path", str(tmp_path / "embed.json"))
    object.__setattr__(SETTINGS, "pid_file", str(tmp_path / "codespine.pid"))
    object.__setattr__(SETTINGS, "log_file", str(tmp_path / "codespine.log"))
    try:
        yield
    finally:
        for key, value in original.items():
            object.__setattr__(SETTINGS, key, value)
