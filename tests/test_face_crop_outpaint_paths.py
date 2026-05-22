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


# ────────────────────────────────────────────────────────────────────────
# Worker pass-shape tests — covers PR #48 round 6 recovery commit.
#
# The recovery flips the worker so it calls ``gen.outpaint(...)`` with
# ``output_path=None`` (matching ``main``'s exact call shape) and then
# performs a post-composite rename onto the planned target. These tests
# pin the contract: the generator is called the SAME WAY ``main`` calls
# it, and the rename happens AFTER the generator returns.
# ────────────────────────────────────────────────────────────────────────


class _FakeOutpaintGen:
    """Fake ``gen.outpaint`` that writes a real placeholder + records calls.

    Mirrors ``outpaint_generator.py``'s auto-naming + collision suffix so
    the worker's post-composite rename step has a real file to move. A
    fake that returned a path string without creating the file would
    make the rename silently no-op and the assertion that pass 2's
    input is the renamed file would falsely pass for the wrong reason.
    """

    def __init__(self, *args, **kwargs):
        self.calls: list[dict] = []

    def set_progress_callback(self, cb):
        pass

    def get_last_outpaint_error_detail(self):
        return ""

    def outpaint(
        self,
        *,
        image_path,
        output_folder,
        output_path=None,
        composite_mode,
        output_format="png",
        **kwargs,
    ):
        if output_path is None:
            stem = Path(image_path).stem
            ext = f".{output_format}"
            out = Path(output_folder) / f"{stem}-expanded{ext}"
            n = 1
            while out.exists():
                out = Path(output_folder) / f"{stem}-expanded_v{n}{ext}"
                n += 1
        else:
            out = Path(output_path)
        out.write_bytes(b"FAKE_OUTPAINT_RESULT")
        self.calls.append({
            "image_path": image_path,
            "output_folder": str(output_folder),
            "output_path": output_path,
            "composite_mode": composite_mode,
            "output_format": output_format,
            **kwargs,
        })
        return str(out)


def _make_fake_expand_tab(
    *,
    tmp_path: Path,
    input_image: Path,
    do_2x: bool,
    composite_mode: str = "preserve_seamless",
):
    """Build a MagicMock-stubbed FaceCropTab able to drive ``_outpaint_image``.

    ``winfo_toplevel().after`` is wired to execute the scheduled lambda
    synchronously so log calls (and the eventual ``_on_outpaint_done``
    dispatch) run inline in the test thread.
    """
    fake = MagicMock()
    fake._outpaint_busy = False
    fake._outpaint_run_token = 0
    fake._outpaint_cancel_event = None
    fake.outpaint_generator = None
    fake.get_config.return_value = {
        "falai_api_key": "k_fal",
        "freeimage_api_key": "k_free",
        "bfl_api_key": "",
        "outpaint_fal_timeout_seconds": 60,
    }
    fake._outpaint_provider_var.get.return_value = "fal"
    fake._get_gen_dir.return_value = tmp_path
    fake._expand_mode_var.get.return_value = "pixels"
    fake._expand_left_var.get.return_value = 50
    fake._expand_right_var.get.return_value = 50
    fake._expand_top_var.get.return_value = 50
    fake._expand_bottom_var.get.return_value = 50
    fake._outpaint_format_var.get.return_value = "png"
    fake._outpaint_prompt_str = ""
    fake._outpaint_double_expand_var.get.return_value = do_2x
    fake._outpaint_composite_var.get.return_value = composite_mode
    fake.image_session.active_image_path = str(input_image)
    fake.image_session.active_entry = MagicMock(ops={})
    fake._find_crop_ref_path.return_value = None  # disable similarity branch

    # Synchronous after-marshal — execute scheduled lambdas inline so log
    # / _on_outpaint_done calls are observable on the fake.
    fake.winfo_toplevel.return_value.after.side_effect = (
        lambda _delay, fn, *args, **kw: fn(*args, **kw)
    )
    return fake


def _run_outpaint_worker(fake, fake_gen, monkeypatch):
    """Invoke ``FaceCropTab._outpaint_image`` synchronously with patched deps."""
    from kling_gui.tabs.face_crop_tab import FaceCropTab
    import outpaint_generator
    import threading

    monkeypatch.setattr(
        outpaint_generator, "OutpaintGenerator", lambda *a, **kw: fake_gen,
    )

    # threading.Thread(...).start() must run the target inline so the
    # worker finishes before the test assertions run.
    class _InlineThread:
        def __init__(self, target=None, daemon=None, **kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _InlineThread)

    FaceCropTab._outpaint_image(fake)


def _log_messages(fake) -> list[tuple[str, str]]:
    """Extract (message, level) tuples from every fake.log call."""
    out = []
    for c in fake.log.call_args_list:
        args = c.args
        if len(args) >= 2:
            out.append((args[0], args[1]))
    return out


def test_1x_passes_preserve_seamless(tmp_path, monkeypatch):
    """1x expand: exactly one gen.outpaint call with composite_mode set
    and output_path=None (recovery: auto-name + post-composite rename)."""
    src = tmp_path / "front.png"
    src.write_bytes(b"INPUT")
    fake = _make_fake_expand_tab(
        tmp_path=tmp_path, input_image=src, do_2x=False,
    )
    fake_gen = _FakeOutpaintGen()
    _run_outpaint_worker(fake, fake_gen, monkeypatch)

    assert len(fake_gen.calls) == 1
    call = fake_gen.calls[0]
    assert call["composite_mode"] == "preserve_seamless"
    assert call["output_path"] is None


def test_2x_both_passes_preserve_seamless(tmp_path, monkeypatch):
    """2x expand: two gen.outpaint calls, BOTH with composite_mode set
    and output_path=None."""
    src = tmp_path / "front.png"
    src.write_bytes(b"INPUT")
    fake = _make_fake_expand_tab(
        tmp_path=tmp_path, input_image=src, do_2x=True,
    )
    fake_gen = _FakeOutpaintGen()
    _run_outpaint_worker(fake, fake_gen, monkeypatch)

    assert len(fake_gen.calls) == 2
    for call in fake_gen.calls:
        assert call["composite_mode"] == "preserve_seamless"
        assert call["output_path"] is None


def test_2x_pass2_input_is_renamed_pass1(tmp_path, monkeypatch):
    """Pass 2's image_path is the planned pass-1 target (the renamed
    file), not the auto-named ``-expanded.png`` that the generator
    would have produced absent the post-composite rename."""
    src = tmp_path / "front.png"
    src.write_bytes(b"INPUT")
    fake = _make_fake_expand_tab(
        tmp_path=tmp_path, input_image=src, do_2x=True,
    )
    fake_gen = _FakeOutpaintGen()
    _run_outpaint_worker(fake, fake_gen, monkeypatch)

    expected_pass1_target = tmp_path / "front-expanded.png"
    assert fake_gen.calls[1]["image_path"] == str(expected_pass1_target)
    assert expected_pass1_target.exists()
    # Pass 2's output (after rename) is the -2x target.
    assert (tmp_path / "front-expanded-2x.png").exists()


def test_2x_partial_pass1_succeeds_pass2_fails(tmp_path, monkeypatch):
    """If pass 1 succeeds and pass 2 fails, pass 1 still reaches the
    carousel via _on_outpaint_done (partial-2x success)."""
    src = tmp_path / "front.png"
    src.write_bytes(b"INPUT")
    fake = _make_fake_expand_tab(
        tmp_path=tmp_path, input_image=src, do_2x=True,
    )

    class _PartialFailGen(_FakeOutpaintGen):
        def outpaint(self, **kwargs):
            if len(self.calls) == 0:
                return super().outpaint(**kwargs)
            self.calls.append({**kwargs, "failed": True})
            return None

    fake_gen = _PartialFailGen()
    _run_outpaint_worker(fake, fake_gen, monkeypatch)

    # _on_outpaint_done was dispatched with one success + total_passes=2.
    fake._on_outpaint_done.assert_called_once()
    per_pass_results, total_passes, *_ = fake._on_outpaint_done.call_args.args
    assert total_passes == 2
    assert len(per_pass_results) == 1
    # Successful result path is the planned pass-1 target.
    assert per_pass_results[0][0] == str(tmp_path / "front-expanded.png")


def test_composite_none_emits_warning(tmp_path, monkeypatch):
    """When composite_mode == 'none', worker logs the explicit
    'composite mode is None' warning at run start but still runs."""
    src = tmp_path / "front.png"
    src.write_bytes(b"INPUT")
    fake = _make_fake_expand_tab(
        tmp_path=tmp_path, input_image=src, do_2x=False,
        composite_mode="none",
    )
    fake_gen = _FakeOutpaintGen()
    _run_outpaint_worker(fake, fake_gen, monkeypatch)

    # Generator still ran.
    assert len(fake_gen.calls) == 1
    assert fake_gen.calls[0]["composite_mode"] == "none"
    # Warning fired.
    warnings = [m for m, lvl in _log_messages(fake) if lvl == "warning"]
    assert any('composite mode is "None"' in m for m in warnings)


def test_planned_targets_for_2x_naming(tmp_path, monkeypatch):
    """Recovery banner logs both planned pass targets, and on-disk
    files match: pass 1 = '<stem>-expanded.png', pass 2 =
    '<stem>-expanded-2x.png'."""
    src = tmp_path / "front.png"
    src.write_bytes(b"INPUT")
    fake = _make_fake_expand_tab(
        tmp_path=tmp_path, input_image=src, do_2x=True,
    )
    fake_gen = _FakeOutpaintGen()
    _run_outpaint_worker(fake, fake_gen, monkeypatch)

    assert (tmp_path / "front-expanded.png").exists()
    assert (tmp_path / "front-expanded-2x.png").exists()

    info_msgs = [m for m, lvl in _log_messages(fake) if lvl == "info"]
    # do_2x + composite banner present.
    assert any(
        "do_2x=True" in m and "composite_mode=preserve_seamless" in m
        for m in info_msgs
    )
    # Both planned targets surfaced.
    assert any("planned pass 1 -> front-expanded.png" in m for m in info_msgs)
    assert any("planned pass 2 -> front-expanded-2x.png" in m for m in info_msgs)
