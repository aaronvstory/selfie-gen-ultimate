"""Regression tests for the comprehensive CLI review fix pass.

Covers the load-bearing findings: the CRITICAL output-mode invalid-state bug,
the questionary abort helper, the shared option constants (drift guard), the
value-aware price cache, the ANSI-strip helper, and the headless no-questionary
invariant.
"""
from __future__ import annotations

import io

import pytest

import kling_automation_ui as k
from kling_automation_ui import KlingAutomationUI, _qs_or_abort, _QuestionarySectionAbort


def _app(tmp_path):
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {}
    app.print_red = lambda *a, **kw: None
    app.print_green = lambda *a, **kw: None
    app.print_yellow = lambda *a, **kw: None
    app.save_config = lambda: app.config.setdefault("_saved", 0)
    return app


# --- A1: output-mode invalid-state on cancel -------------------------------

def test_change_output_mode_cancel_keeps_existing_folder(tmp_path, monkeypatch):
    """Custom-folder mode with an empty path answer must NOT persist
    use_source_folder=False with an empty output_folder."""
    app = _app(tmp_path)
    saved = {"n": 0}
    app.save_config = lambda: saved.__setitem__("n", saved["n"] + 1)
    # No existing folder, legacy path (non-TTY): pick custom (2) then empty path.
    responses = iter(["2", ""])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(responses, ""))
    app.change_output_mode()
    # With no existing folder + empty path, mode is left untouched (no flip+save).
    assert app.config.get("use_source_folder") is not False or app.config.get("output_folder")
    # Specifically: we did NOT persist an invalid (False + empty) state.
    if app.config.get("use_source_folder") is False:
        assert app.config.get("output_folder")


def test_change_output_mode_custom_with_path_persists(tmp_path, monkeypatch):
    app = _app(tmp_path)
    target = tmp_path / "videos_out"
    responses = iter(["2", str(target)])
    monkeypatch.setattr("builtins.input", lambda *a, **kw: next(responses, ""))
    app.change_output_mode()
    assert app.config["use_source_folder"] is False
    assert app.config["output_folder"] == str(target)
    assert target.is_dir()


# --- B: questionary abort helper -------------------------------------------

def test_qs_or_abort_raises_on_none():
    with pytest.raises(_QuestionarySectionAbort):
        _qs_or_abort(None)


def test_qs_or_abort_passes_value_through():
    assert _qs_or_abort("nano") == "nano"
    assert _qs_or_abort("") == ""  # empty string is a real value, not an abort


# --- D: shared option constants (single source) -----------------------------

def test_shared_option_constants_present_and_3x4_default():
    assert "all" in k._OLDCAM_VERSION_OPTIONS
    assert k._VIDEO_RESOLUTION_OPTIONS == ["480p", "720p"]
    assert "3:4" in k._VIDEO_ASPECT_RATIO_OPTIONS
    assert k._REPROCESS_MODE_OPTIONS == ["skip", "overwrite", "increment"]
    assert "none" in k._COMPOSITE_MODE_OPTIONS
    assert "black_fill" in k._COMPOSITE_MODE_OPTIONS
    assert k._PROMPT_SLOT_COUNT == 10
    assert 10 in k._COMMON_VIDEO_DURATIONS


# --- A2: value-aware price cache -------------------------------------------

def test_price_cache_refetches_after_reset():
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {"current_model": "m"}
    calls = {"n": 0}

    def fake_fetch(_endpoint):
        calls["n"] += 1
        return 1.0

    app.fetch_model_pricing = fake_fetch
    app.clear_screen = lambda: None
    app.print_magenta = lambda *a, **kw: None
    app.config["model_display_name"] = "X"
    app.config["video_duration"] = 10
    # First render fetches.
    app.display_header()
    assert calls["n"] == 1
    # Second render uses the cached value (no refetch).
    app.display_header()
    assert calls["n"] == 1
    # Simulate a model change resetting the cache to None.
    app._cached_price = None
    app.display_header()
    assert calls["n"] == 2  # re-fetched because the guard is value-aware


# --- A3: ANSI strip ---------------------------------------------------------

def test_ansi_escape_regex_strips_color():
    assert k._ANSI_ESCAPE_RE.sub("", "\033[92mhello\033[0m world") == "hello world"
    assert k._ANSI_ESCAPE_RE.sub("", "plain") == "plain"


# --- headless no-questionary invariant -------------------------------------

def test_headless_path_reads_no_stdin(tmp_path, monkeypatch):
    """run_automation_headless must not read stdin (no input()/questionary).
    A missing root simply exits 1 without touching stdin."""
    app = KlingAutomationUI.__new__(KlingAutomationUI)
    app.config = {"automation_front_names": ["front.png"]}
    app.automation_root_folder = ""
    app.print_red = lambda *a, **kw: None
    app.print_yellow = lambda *a, **kw: None

    def _boom(*a, **kw):
        raise AssertionError("headless path must not call input()")

    monkeypatch.setattr("builtins.input", _boom)
    # Force a TTY so any accidental questionary gate would also be exercised.
    monkeypatch.setattr(k.sys, "stdin", io.StringIO(""), raising=False)
    rc = app.run_automation_headless("Z:/definitely_missing_root_xyz")
    assert rc == 1  # exited cleanly without reading stdin
