"""Path-planning tests for Step 0 (Face Crop tab) Generative Expand outputs.

Exercises ``kling_gui.tag_utils.build_expand_filenames``, the pure function
that decides where pass 1 and pass 2 of a Step 0 expand run land on disk.

Naming contract (set by user request 2026-05-22):
  - Single-pass run (do_2x=False) writes only ``<stem>-expanded<ext>``.
  - 2x run writes ``<stem>-expanded<ext>`` for pass 1 AND
    ``<stem>-expanded-2x<ext>`` for pass 2.
  - Collisions are resolved per-path with a ``_v2``, ``_v3`` ... suffix
    so the ``-expanded`` / ``-expanded-2x`` suffix stays intact even on
    repeated re-runs.
"""

from pathlib import Path

import pytest

from kling_gui.tag_utils import build_expand_filenames


def test_single_pass_no_collision(tmp_path: Path):
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=False,
    )
    assert p1 == tmp_path / "front-expanded.png"
    assert p2 is None


def test_two_pass_no_collision(tmp_path: Path):
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    assert p1 == tmp_path / "front-expanded.png"
    assert p2 == tmp_path / "front-expanded-2x.png"


def test_two_pass_both_collide(tmp_path: Path):
    (tmp_path / "front-expanded.png").write_bytes(b"x")
    (tmp_path / "front-expanded-2x.png").write_bytes(b"x")
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    assert p1 == tmp_path / "front-expanded_v2.png"
    assert p2 == tmp_path / "front-expanded-2x_v2.png"


def test_two_pass_only_pass1_collides(tmp_path: Path):
    """In paired-collision mode (subagent M2 fix), if EITHER pass 1 or
    pass 2 collides, BOTH get the same ``_vN`` suffix so the on-disk
    pair stays semantically linked."""
    (tmp_path / "front-expanded.png").write_bytes(b"x")
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    assert p1 == tmp_path / "front-expanded_v2.png"
    assert p2 == tmp_path / "front-expanded-2x_v2.png"


def test_two_pass_only_pass2_collides(tmp_path: Path):
    """Same paired behavior, triggered by pass-2 collision."""
    (tmp_path / "front-expanded-2x.png").write_bytes(b"x")
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    assert p1 == tmp_path / "front-expanded_v2.png"
    assert p2 == tmp_path / "front-expanded-2x_v2.png"


def test_two_pass_pairs_skip_through_partial_collisions(tmp_path: Path):
    """If pass1 is free at v1, v2, v3 but pass2 collides at v1 and v3, the
    pair must advance to the smallest n where BOTH are free."""
    (tmp_path / "front-expanded-2x.png").write_bytes(b"x")        # v1
    (tmp_path / "front-expanded_v2.png").write_bytes(b"x")        # v2 (pass1 only)
    (tmp_path / "front-expanded-2x_v3.png").write_bytes(b"x")     # v3
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    # v4 is the first index free for BOTH.
    assert p1 == tmp_path / "front-expanded_v4.png"
    assert p2 == tmp_path / "front-expanded-2x_v4.png"


def test_two_pass_deep_collision_chain(tmp_path: Path):
    for n in range(2, 5):
        (tmp_path / f"front-expanded_v{n}.png").write_bytes(b"x")
    (tmp_path / "front-expanded.png").write_bytes(b"x")
    p1, _ = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=False,
    )
    # First free slot is _v5 (collision-resolver walks 2, 3, 4, then 5).
    assert p1 == tmp_path / "front-expanded_v5.png"


def test_dot_prefix_in_ext_ignored(tmp_path: Path):
    """The helper should accept either ``"png"`` or ``".png"`` as ext."""
    p_with = build_expand_filenames("front", "png", tmp_path, False)[0]
    p_without = build_expand_filenames("front", ".png", tmp_path, False)[0]
    assert p_with == p_without == tmp_path / "front-expanded.png"


def test_already_expanded_stem(tmp_path: Path):
    """Re-expanding an already-expanded image stacks the suffix.

    This matches the existing outpaint_generator auto-name behavior. We
    don't try to detect "already expanded" stems and avoid the stacking —
    that would be a separate UX decision.
    """
    p1, p2 = build_expand_filenames(
        base_stem="front-expanded", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    assert p1 == tmp_path / "front-expanded-expanded.png"
    assert p2 == tmp_path / "front-expanded-expanded-2x.png"


def test_jpeg_extension(tmp_path: Path):
    p1, p2 = build_expand_filenames("hero", "jpeg", tmp_path, True)
    assert p1 == tmp_path / "hero-expanded.jpeg"
    assert p2 == tmp_path / "hero-expanded-2x.jpeg"


def test_returns_pathlib_paths(tmp_path: Path):
    p1, p2 = build_expand_filenames("front", "png", tmp_path, True)
    assert isinstance(p1, Path)
    assert isinstance(p2, Path)


def test_gen_dir_accepts_string_path(tmp_path: Path):
    """gen_dir argument may be a str — common at call sites that haven't
    migrated to pathlib internally."""
    p1, _ = build_expand_filenames("front", "png", str(tmp_path), False)
    assert p1 == tmp_path / "front-expanded.png"


def test_unsafe_stem_sanitized(tmp_path: Path):
    """A stem with path separators or shell metas falls back via
    ``sanitize_stem`` to a safe form. Verify we don't crash and we don't
    create a path outside ``gen_dir``."""
    p1, _ = build_expand_filenames(
        base_stem="../etc/passwd",
        ext="png",
        gen_dir=tmp_path,
        do_2x=False,
    )
    # Path must be inside gen_dir
    assert tmp_path in p1.parents
    # ... and end with the -expanded suffix
    assert p1.name.endswith("-expanded.png")


# ────────────────────────────────────────────────────────────────────────
# _on_outpaint_done state-transition tests — covers code-review M3/H1/H2/L6
# on subagent ae2dd01f. Uses MagicMock to avoid the cost of constructing
# a real Tk root + FaceCropTab subclass for what are essentially pure
# state-transition assertions.
# ────────────────────────────────────────────────────────────────────────

from unittest.mock import MagicMock


def _make_fake_tab(token: int = 1, cancelled: bool = False):
    """Bare-minimum stub for invoking ``FaceCropTab._on_outpaint_done``."""
    from kling_gui.tabs.face_crop_tab import FaceCropTab  # noqa: F401 — sanity check import
    fake = MagicMock()
    fake._outpaint_run_token = token
    if cancelled:
        fake._outpaint_cancel_event = MagicMock()
        fake._outpaint_cancel_event.is_set.return_value = True
    else:
        fake._outpaint_cancel_event = None
    fake.outpaint_generator = None
    return fake


def _call_done(fake, per_pass_results, total_passes, run_token=1):
    from kling_gui.tabs.face_crop_tab import FaceCropTab
    return FaceCropTab._on_outpaint_done(
        fake, per_pass_results, total_passes, run_token=run_token,
    )


def _last_status_text(fake) -> str:
    """Extract the most-recent text= kwarg passed to _outpaint_status.config."""
    calls = fake._outpaint_status.config.call_args_list
    for call in reversed(calls):
        kwargs = call.kwargs
        if "text" in kwargs:
            return kwargs["text"]
    return ""


def test_done_full_success_1x():
    fake = _make_fake_tab()
    _call_done(fake, [("front-expanded.png", "85%", {"exp": 1})], total_passes=1)
    fake.image_session.add_image.assert_called_once()
    assert _last_status_text(fake).startswith("Done:")
    # No warning log (only the per-pass "saved" success log).
    log_levels = [c.args[1] for c in fake.log.call_args_list if len(c.args) >= 2]
    assert "warning" not in log_levels
    assert "error" not in log_levels


def test_done_full_success_2x():
    fake = _make_fake_tab()
    _call_done(
        fake,
        [
            ("front-expanded.png", "85%", {"exp": 1}),
            ("front-expanded-2x.png", "82%", {"exp": 2}),
        ],
        total_passes=2,
    )
    assert fake.image_session.add_image.call_count == 2
    assert _last_status_text(fake).startswith("Done:")
    # Status name reflects the FINAL pass.
    assert "expanded-2x" in _last_status_text(fake)


def test_done_partial_2x_pass2_failed():
    """H1 fix: pass 1 OK, pass 2 failed → 'Partial' status + warning log."""
    fake = _make_fake_tab()
    _call_done(
        fake,
        [("front-expanded.png", "85%", {"exp": 1})],
        total_passes=2,
    )
    # Pass 1 still added to carousel.
    fake.image_session.add_image.assert_called_once()
    # Status reads as partial, not done.
    status = _last_status_text(fake)
    assert status.startswith("Partial:")
    assert "1/2" in status
    assert "pass 2 failed" in status
    # Warning log emitted (along with the per-pass success log).
    log_levels = [c.args[1] for c in fake.log.call_args_list if len(c.args) >= 2]
    assert "warning" in log_levels


def test_done_cancel_keeps_pass1_in_carousel():
    """H2 fix: abort mid-2x AFTER pass 1 succeeded must still add pass 1
    to the carousel (otherwise the on-disk file is orphaned)."""
    fake = _make_fake_tab(cancelled=True)
    _call_done(
        fake,
        [("front-expanded.png", "85%", {"exp": 1})],
        total_passes=2,
    )
    # Pass 1 added BEFORE the cancel short-circuit.
    fake.image_session.add_image.assert_called_once()
    assert _last_status_text(fake) == "Aborted by user"
    # Warning log mentions the kept pass count.
    warning_msgs = [
        c.args[0] for c in fake.log.call_args_list
        if len(c.args) >= 2 and c.args[1] == "warning"
    ]
    assert any("kept 1 successful pass" in m for m in warning_msgs)


def test_done_cancel_with_zero_results():
    """Cancel + nothing committed yet → just "Aborted by user", no
    spurious add_image, plain warning log (no "kept N" message)."""
    fake = _make_fake_tab(cancelled=True)
    _call_done(fake, [], total_passes=2)
    fake.image_session.add_image.assert_not_called()
    assert _last_status_text(fake) == "Aborted by user"
    warning_msgs = [
        c.args[0] for c in fake.log.call_args_list
        if len(c.args) >= 2 and c.args[1] == "warning"
    ]
    assert "Expand aborted by user" in warning_msgs


def test_done_all_passes_failed():
    fake = _make_fake_tab()
    _call_done(fake, [], total_passes=1)
    fake.image_session.add_image.assert_not_called()
    assert _last_status_text(fake) == "Failed"
    log_levels = [c.args[1] for c in fake.log.call_args_list if len(c.args) >= 2]
    assert "error" in log_levels


def test_done_stale_run_token_no_op():
    """If the user clicked Expand twice quickly, the OLD worker's
    callback fires with a stale token and must not touch the UI."""
    fake = _make_fake_tab(token=5)
    _call_done(
        fake,
        [("front-expanded.png", "85%", {"exp": 1})],
        total_passes=1,
        run_token=3,  # stale
    )
    fake.image_session.add_image.assert_not_called()
    fake._outpaint_status.config.assert_not_called()
