"""Regression tests for `_resolve_live_crop_ref` added in
PR fix/step0-composite-and-rppg-v2.5.

The bug: `_find_crop_ref_path()` returned a cached entry path that
the per-pass similarity check trusted blindly. If the file vanished
between scan and use, `compute_face_similarity` raised FileNotFoundError
which surfaced as `Sim: File not found: ...` warnings.

The fix:
1. `_resolve_live_crop_ref` verifies on-disk presence right now and
   falls back through `_last_crop_path` then a `gen-images/*_crop.*`
   glob.
2. Per-pass code calls `_resolve_live_crop_ref` immediately before
   each `compute_face_similarity` call (not once at worker start).
3. Missing ref logs at debug, not warning.

These tests cover the resolver logic by exercising the helper
directly with a minimal stub object — instantiating the full
FaceCropTab requires Tk + numerous dependencies and is fragile in CI.
"""

import os
from pathlib import Path
from types import SimpleNamespace


def _make_resolver_stub(entries, last_crop_path, gen_dir):
    """Build a minimal stub with just enough surface for
    _resolve_live_crop_ref to bind to.
    """
    from kling_gui.tabs.face_crop_tab import FaceCropTab

    stub = SimpleNamespace()
    stub.image_session = SimpleNamespace(images=entries)
    stub._last_crop_path = last_crop_path
    stub._get_gen_dir = lambda: gen_dir
    # Bind the unbound method to the stub
    stub._resolve_live_crop_ref = FaceCropTab._resolve_live_crop_ref.__get__(
        stub, type(stub)
    )
    return stub


def _make_entry(path: str, source_type: str = "input"):
    """Build a fake ImageEntry that the resolver can iterate. We
    only need `path`, `source_type`, `filename` for the search loop.
    The `exists` property is a getter on the real ImageEntry — here
    it's just a plain attribute so the test can simulate either state.
    """
    e = SimpleNamespace()
    e.path = path
    e.source_type = source_type
    e.filename = os.path.basename(path)
    return e


def test_resolver_returns_entry_when_on_disk(tmp_path: Path):
    crop = tmp_path / "front_crop.jpg"
    crop.write_bytes(b"jpg")
    entries = [_make_entry(str(crop))]
    stub = _make_resolver_stub(entries, None, tmp_path)
    assert stub._resolve_live_crop_ref() == str(crop)


def test_resolver_falls_back_to_last_crop_path_when_entry_missing(
    tmp_path: Path,
):
    """Session entry path doesn't exist on disk; _last_crop_path does."""
    stale = tmp_path / "missing_crop.jpg"  # never created
    last_crop = tmp_path / "real_crop.jpg"
    last_crop.write_bytes(b"jpg")
    entries = [_make_entry(str(stale))]
    stub = _make_resolver_stub(entries, str(last_crop), tmp_path)
    assert stub._resolve_live_crop_ref() == str(last_crop)


def test_resolver_falls_back_to_gen_images_glob(tmp_path: Path):
    """Neither session entries nor _last_crop_path point to an
    on-disk file, but a `*_crop.*` exists in gen_dir.
    """
    gen_dir = tmp_path / "gen-images"
    gen_dir.mkdir()
    glob_match = gen_dir / "front_crop.jpg"
    glob_match.write_bytes(b"jpg")
    stub = _make_resolver_stub([], None, gen_dir)
    assert stub._resolve_live_crop_ref() == str(glob_match)


def test_resolver_returns_none_when_nothing_on_disk(tmp_path: Path):
    """All three fallback steps fail -> None. Caller is expected to
    skip similarity at debug level, NOT warning.
    """
    stale = tmp_path / "missing.jpg"  # never created
    entries = [_make_entry(str(stale))]
    stub = _make_resolver_stub(
        entries,
        str(tmp_path / "also_missing.jpg"),
        tmp_path,  # empty dir, no *_crop.* matches
    )
    assert stub._resolve_live_crop_ref() is None


def test_resolver_ignores_non_input_entries(tmp_path: Path):
    """Only `input` type entries with _crop in the filename are
    considered. A selfie/outpaint entry named something_crop.jpg
    should not be picked up.
    """
    decoy = tmp_path / "selfie_crop.jpg"
    decoy.write_bytes(b"jpg")
    real = tmp_path / "front_crop.jpg"
    real.write_bytes(b"jpg")
    entries = [
        _make_entry(str(decoy), source_type="selfie"),
        _make_entry(str(real), source_type="input"),
    ]
    stub = _make_resolver_stub(entries, None, tmp_path)
    assert stub._resolve_live_crop_ref() == str(real)


def test_resolver_ignores_entries_without_crop_in_name(tmp_path: Path):
    """Even an `input` entry whose name lacks `_crop` is not picked
    (e.g. the original source image).
    """
    source = tmp_path / "front.jpg"
    source.write_bytes(b"jpg")
    real = tmp_path / "front_crop.jpg"
    real.write_bytes(b"jpg")
    entries = [
        _make_entry(str(source), source_type="input"),
        _make_entry(str(real), source_type="input"),
    ]
    stub = _make_resolver_stub(entries, None, tmp_path)
    assert stub._resolve_live_crop_ref() == str(real)
