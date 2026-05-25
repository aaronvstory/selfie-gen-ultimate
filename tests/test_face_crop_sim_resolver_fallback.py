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
    # Active source stem helps stem-filter the glob.
    stub._source_path = str(tmp_path / "front.jpg")
    assert stub._resolve_live_crop_ref() == str(glob_match)


def test_resolver_glob_prefers_active_source_stem(tmp_path: Path):
    """PR #53 round 2 — subagent H4. With multiple sources in the same
    folder, the glob must prefer the active source's `{stem}_crop.*`
    over alphabetic-first ANY `*_crop.*`. Otherwise bob's expand would
    silently compare against alice's crop (alice < bob alphabetically).
    """
    gen_dir = tmp_path / "gen-images"
    gen_dir.mkdir()
    alice_crop = gen_dir / "alice_crop.jpg"
    alice_crop.write_bytes(b"alice")
    bob_crop = gen_dir / "bob_crop.jpg"
    bob_crop.write_bytes(b"bob")
    stub = _make_resolver_stub([], None, gen_dir)
    # Active source is bob.jpg -> resolver should pick bob_crop.jpg
    # NOT alice_crop.jpg (alphabetic first).
    stub._source_path = str(tmp_path / "bob.jpg")
    assert stub._resolve_live_crop_ref() == str(bob_crop)


def test_resolver_refuses_generic_fallback_when_multiple_crops_present(
    tmp_path: Path,
):
    """PR #53 round 5 H3: when the active source's stem doesn't match
    ANY gen-images crop AND there are multiple `*_crop.*` siblings,
    REFUSE to pick (silent wrong-identity scoring is worse than
    skipping similarity entirely). Returns None — caller logs skip
    at debug.

    This INVERTS the previous round-3 behavior which returned the
    mtime-newest in that case — subagent (round 5) flagged that
    behavior was actively unsafe in shared `gen-images/` folders.
    """
    import os as _os
    gen_dir = tmp_path / "gen-images"
    gen_dir.mkdir()
    alice = gen_dir / "alice_crop.jpg"
    alice.write_bytes(b"alice")
    bob = gen_dir / "bob_crop.jpg"
    bob.write_bytes(b"bob")
    _os.utime(alice, (1_000_000, 1_000_000))
    _os.utime(bob, (2_000_000, 2_000_000))
    stub = _make_resolver_stub([], None, gen_dir)
    # Active source stem matches NEITHER crop AND prefix-relaxed
    # ("charlie" -> "charlie") also doesn't match -> resolver
    # returns None instead of guessing which subject to score against.
    stub._source_path = str(tmp_path / "charlie.jpg")
    assert stub._resolve_live_crop_ref() is None


def test_resolver_uses_generic_fallback_when_exactly_one_crop_present(
    tmp_path: Path,
):
    """PR #53 round 5 H3: when the active source's stem doesn't
    match but there's EXACTLY ONE `*_crop.*` in the folder, the
    resolver returns it — this is the single-subject-folder case
    where the user moved/renamed the source file but the crop is
    still uniquely identifiable.
    """
    gen_dir = tmp_path / "gen-images"
    gen_dir.mkdir()
    only = gen_dir / "alice_crop.jpg"
    only.write_bytes(b"alice")
    stub = _make_resolver_stub([], None, gen_dir)
    stub._source_path = str(tmp_path / "charlie.jpg")
    assert stub._resolve_live_crop_ref() == str(only)


def test_resolver_prefix_relaxed_stem_match(tmp_path: Path):
    """PR #53 round 5 H3: when the active source is a derived
    artifact like ``alice-expanded.jpg``, the exact-stem pattern
    (``alice-expanded_crop.*``) misses. The resolver tries a
    prefix-relaxed pattern using the first hyphen-split segment
    (``alice_crop.*``) before falling through to the strict
    single-crop generic fallback.
    """
    gen_dir = tmp_path / "gen-images"
    gen_dir.mkdir()
    alice = gen_dir / "alice_crop.jpg"
    alice.write_bytes(b"alice")
    # Decoy in same folder — generic fallback would refuse, so the
    # prefix-relaxed match has to actually hit for this test to
    # pass.
    bob = gen_dir / "bob_crop.jpg"
    bob.write_bytes(b"bob")
    stub = _make_resolver_stub([], None, gen_dir)
    stub._source_path = str(tmp_path / "alice-expanded.jpg")
    assert stub._resolve_live_crop_ref() == str(alice)


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


def test_resolver_survives_entry_with_none_path(tmp_path: Path):
    """PR #53 round 4 (reviewer feedback): an entry whose .path was
    upstream-set to None (or any non-str) used to raise TypeError on
    Path() construction, which then escaped the broad-except below.
    The resolver now explicitly bool-checks entry.path AND catches
    TypeError on Path() so the loop continues safely.
    """
    # Create one valid crop so the resolver eventually returns
    # something — the test specifically checks the bad entry doesn't
    # crash the iteration.
    real = tmp_path / "front_crop.jpg"
    real.write_bytes(b"jpg")
    bad_entry = _make_entry(str(real))
    bad_entry.path = None  # type: ignore[assignment]
    bad_entry.filename = "ghost_crop.jpg"  # so the _crop filter passes
    good_entry = _make_entry(str(real))
    entries = [bad_entry, good_entry]
    stub = _make_resolver_stub(entries, None, tmp_path)
    # Should not raise; should return the good entry's path.
    assert stub._resolve_live_crop_ref() == str(real)


def test_resolver_glob_escapes_stem_metacharacters(tmp_path: Path):
    """PR #53 round 4 (reviewer feedback): a source stem containing
    ``[`` or ``]`` (e.g. ``selfie[final].jpg``) used to be interpreted
    by Path.glob as a character class, missing the literal-named
    ``selfie[final]_crop.jpg``. ``glob.escape`` fixes the match.
    """
    gen_dir = tmp_path / "gen-images"
    gen_dir.mkdir()
    real = gen_dir / "selfie[final]_crop.jpg"
    real.write_bytes(b"jpg")
    # Also drop a decoy that WOULD match an unescaped char-class glob:
    decoy = gen_dir / "selfief_crop.jpg"  # 'f' is in [final]
    decoy.write_bytes(b"decoy")
    stub = _make_resolver_stub([], None, gen_dir)
    stub._source_path = str(tmp_path / "selfie[final].jpg")
    # Must return the literal-stem match, not the char-class match.
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
