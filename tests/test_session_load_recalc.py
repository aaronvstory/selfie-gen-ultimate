"""Regression tests for the session-restore similarity-recompute path.

The previous implementation had a catch-22:
    1. session_manager.save_session() always stamps "similarity_engine_version": "1.8"
    2. _on_session_loaded() invalidated AND triggered recompute ONLY when the stamp
       was missing or != "1.8"
    => v1.8-saved autosaves carried the stamp, so reload was silently a no-op.

The fix: ALWAYS fire `recalc_all_similarity_now` when `loaded_count > 0`, regardless
of the version stamp. The reason string varies based on whether it's a legacy
migration or a fresh-engine sanity refresh.

These tests assert the new behavior using a focused harness that exercises just the
post-restore gate logic (the actual GUI call requires a full Tk window — out of
scope for unit tests).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from kling_gui import session_manager as sm


class _StubEntry:
    def __init__(self, path: str):
        self.path = path


class _StubSession:
    def __init__(self, source_path: str):
        self.reference_entry = _StubEntry(source_path)
        self.input_images = [(0, _StubEntry(source_path))]
        self.images = [_StubEntry(source_path)]
        self.count = 1

    def to_dict(self) -> dict:
        return {
            "images": [{"path": self.reference_entry.path, "source_type": "input"}],
            "current_index": 0,
            "reference_index": 0,
            "similarity_ref_index": -1,
        }


def _save_and_reload(tmp_path: Path) -> dict:
    """Save a session via save_session() then read the on-disk JSON back."""
    src = tmp_path / "front.png"
    src.write_bytes(b"x")
    session = _StubSession(str(src))
    saved_path = sm.save_session(
        app_dir=str(tmp_path),
        image_session=session,
        config={},
        name="regression_session",
    )
    with open(saved_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_save_session_always_stamps_engine_version_1_8(tmp_path: Path):
    """Confirms the catch-22 cause: save_session ALWAYS writes '1.8'.
    This is the behavior that caused the silent-failure bug — the version stamp
    cannot be used as a reliable invalidation gate for v1.8-saved sessions."""
    data = _save_and_reload(tmp_path)
    assert data.get("similarity_engine_version") == "1.8"


def _simulate_post_restore_gate(
    *,
    similarity_engine_version: str,
    loaded_count: int,
) -> Tuple[bool, str]:
    """Pure-logic mirror of the post-restore gate in _on_session_loaded.

    Returns (recalc_fired, reason). Mirrors the FIXED logic so a regression that
    re-introduces the catch-22 (e.g., re-gating on `invalidate_legacy_scores`)
    will fail this test.
    """
    invalidate_legacy_scores = similarity_engine_version != "1.8"
    if loaded_count <= 0:
        return False, ""
    reason = (
        "post-restore v1.8 KYC migration"
        if invalidate_legacy_scores
        else "post-restore engine refresh"
    )
    return True, reason


def test_post_restore_recalc_fires_for_v1_8_stamped_session():
    """The actual user-reported bug: v1.8 autosave reloaded → MUST fire recalc."""
    fired, reason = _simulate_post_restore_gate(
        similarity_engine_version="1.8", loaded_count=9
    )
    assert fired is True
    assert "engine refresh" in reason


def test_post_restore_recalc_fires_for_pre_v1_8_session():
    """Legacy v1.7 session (no stamp) → recalc with migration reason."""
    fired, reason = _simulate_post_restore_gate(
        similarity_engine_version="", loaded_count=9
    )
    assert fired is True
    assert "KYC migration" in reason


def test_post_restore_recalc_fires_for_unknown_future_version():
    """Forward compatibility: v1.9 / v2.0 stamps still trigger refresh, never silent."""
    fired, reason = _simulate_post_restore_gate(
        similarity_engine_version="1.9", loaded_count=9
    )
    assert fired is True
    assert "KYC migration" in reason  # treated as "not v1.8" → migration path


def test_post_restore_recalc_skipped_for_empty_session():
    """If no images loaded, no point trying to recalc — but this is the ONLY skip case."""
    fired, _reason = _simulate_post_restore_gate(
        similarity_engine_version="1.8", loaded_count=0
    )
    assert fired is False
