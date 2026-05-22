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
    (tmp_path / "front-expanded.png").write_bytes(b"x")
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    # Pass 1 gets the _v2 suffix; pass 2 stays clean (independent resolve).
    assert p1 == tmp_path / "front-expanded_v2.png"
    assert p2 == tmp_path / "front-expanded-2x.png"


def test_two_pass_only_pass2_collides(tmp_path: Path):
    (tmp_path / "front-expanded-2x.png").write_bytes(b"x")
    p1, p2 = build_expand_filenames(
        base_stem="front", ext="png", gen_dir=tmp_path, do_2x=True,
    )
    assert p1 == tmp_path / "front-expanded.png"
    assert p2 == tmp_path / "front-expanded-2x_v2.png"


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
