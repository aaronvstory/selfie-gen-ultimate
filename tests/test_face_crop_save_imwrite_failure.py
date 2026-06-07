"""Behavioral guard: FaceCropTab._save_crop must treat a falsy cv2.imwrite
return as a write FAILURE (Codex PR #91 MEDIUM).

THE BUG: ``cv2.imwrite()`` returns ``False`` on failure (MAX_PATH overflow,
full disk, bad codec) instead of raising. The original ``_save_crop`` wrapped
the call in ``try/except Exception``, so a ``False`` return:
  1. NEVER triggered the per-instance scratch fallback (the v2.29 MAX_PATH fix
     was effectively dead code for the most common failure modes), and
  2. fell through to log "saved ✓" and add a NONEXISTENT path to the session.

The fix routes both the primary and scratch writes through an ``_try_write``
helper that treats ``not cv2.imwrite(...)`` (and a missing output file) as
failure — matching the established ``face_crop_service.extract_portrait_crop``
convention. These tests pin that behavior:
  * primary write returns False  -> scratch fallback is attempted
  * BOTH return False            -> returns None, logs an error, adds nothing
  * primary write succeeds       -> happy path still works
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

# cv2/numpy are import-time deps of the tab module; skip cleanly if absent.
face_crop_tab = pytest.importorskip("kling_gui.tabs.face_crop_tab")
FaceCropTab = face_crop_tab.FaceCropTab


class _SessionStub:
    def __init__(self):
        self.added = []
        self.similarity_ref_index = 0  # non -1 so the auto-ref branch is skipped
        self.count = 0

    def add_image(self, path, source_type, **kwargs):
        self.added.append((path, source_type))
        self.count += 1

    def set_similarity_ref(self, idx):
        pass


def _make_stub(tmp_path: Path):
    """A minimal object the unbound _save_crop can run against."""
    stub = FaceCropTab.__new__(FaceCropTab)  # bypass Tk __init__
    stub._crop_result = np.zeros((40, 30, 3), dtype=np.uint8)
    stub._source_path = str(tmp_path / "front.png")
    stub._original_path = str(tmp_path / "front.png")
    stub.image_session = _SessionStub()
    stub._last_crop_path = None
    stub._logs = []
    stub.log = lambda msg, level="info": stub._logs.append((level, msg))
    return stub


def test_save_crop_falls_back_to_scratch_when_primary_imwrite_returns_false(
    tmp_path, monkeypatch
):
    """Primary gen-images write returns False (not raises) -> the scratch
    fallback must be attempted, and a successful scratch write returns its
    path. Previously the False return skipped the fallback entirely."""
    stub = _make_stub(tmp_path)
    monkeypatch.setattr(
        face_crop_tab, "get_gen_images_folder", lambda _p: str(tmp_path / "gen")
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        face_crop_tab, "get_runtime_scratch_dir", lambda: str(scratch)
    )

    calls = {"n": 0}

    def fake_imwrite(path, _img):
        calls["n"] += 1
        # First call (gen-images) "fails" by returning False; scratch succeeds.
        if calls["n"] == 1:
            return False
        Path(path).write_bytes(b"jpegbytes")
        return True

    monkeypatch.setattr(face_crop_tab.cv2, "imwrite", fake_imwrite)

    result = FaceCropTab._save_crop(stub)

    assert result is not None, "scratch fallback should have produced a path"
    assert str(result).startswith(str(scratch)), "must land in the scratch dir"
    assert result.exists()
    # The session got the real (existing) scratch path, not a phantom one.
    assert stub.image_session.added == [(str(result), "input")]


def test_save_crop_returns_none_when_both_writes_return_false(
    tmp_path, monkeypatch
):
    """Both gen-images AND scratch writes return False -> _save_crop returns
    None, logs an error, and adds NOTHING to the session (no phantom path)."""
    stub = _make_stub(tmp_path)
    monkeypatch.setattr(
        face_crop_tab, "get_gen_images_folder", lambda _p: str(tmp_path / "gen")
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(
        face_crop_tab, "get_runtime_scratch_dir", lambda: str(scratch)
    )
    monkeypatch.setattr(face_crop_tab.cv2, "imwrite", lambda _p, _i: False)

    result = FaceCropTab._save_crop(stub)

    assert result is None
    assert stub.image_session.added == [], "no phantom path may be added"
    assert any(lvl == "error" for lvl, _ in stub._logs), "must log an error"


def test_save_crop_happy_path_writes_and_adds(tmp_path, monkeypatch):
    """Sanity: a successful primary write returns the gen-images path and
    adds it to the session."""
    stub = _make_stub(tmp_path)
    gen = tmp_path / "gen"
    monkeypatch.setattr(
        face_crop_tab, "get_gen_images_folder", lambda _p: str(gen)
    )
    monkeypatch.setattr(
        face_crop_tab, "get_runtime_scratch_dir", lambda: str(tmp_path / "scratch")
    )

    def fake_imwrite(path, _img):
        Path(path).write_bytes(b"jpegbytes")
        return True

    monkeypatch.setattr(face_crop_tab.cv2, "imwrite", fake_imwrite)

    result = FaceCropTab._save_crop(stub)

    assert result is not None
    assert str(result).startswith(str(gen))
    assert result.exists()
    assert stub.image_session.added == [(str(result), "input")]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
