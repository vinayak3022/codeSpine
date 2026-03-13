"""Dirty overlay index support."""

from codespine.overlay.git_state import current_head, git_repo_root
from codespine.overlay.merge import (
    merged_call_edges,
    merged_class_records,
    merged_method_records,
    merged_symbol_records,
    overlay_summary,
)
from codespine.overlay.store import OverlayStore, build_overlay_file_entry

__all__ = [
    "OverlayStore",
    "build_overlay_file_entry",
    "current_head",
    "git_repo_root",
    "merged_call_edges",
    "merged_class_records",
    "merged_method_records",
    "merged_symbol_records",
    "overlay_summary",
]
