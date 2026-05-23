"""Unit tests for the macOS-only padding helpers in ``kling_gui.theme``.

Covers ``mac_padding``, ``macos_widget_pad``, and the ``CLICK_DEBUG`` /
``attach_click_diagnostics`` opt-in diagnostics.

The helpers are pure — no Tk root needed — so we patch ``IS_MACOS`` /
``CLICK_DEBUG`` directly via ``monkeypatch.setattr``.
"""

import os
import importlib

import pytest

from kling_gui import theme


def test_mac_padding_returns_macos_when_is_macos_true(monkeypatch):
    monkeypatch.setattr(theme, "IS_MACOS", True)
    assert theme.mac_padding((6, 3), (11, 7)) == (11, 7)


def test_mac_padding_returns_default_when_is_macos_false(monkeypatch):
    monkeypatch.setattr(theme, "IS_MACOS", False)
    assert theme.mac_padding((6, 3), (11, 7)) == (6, 3)


def test_mac_padding_default_unchanged_across_calls(monkeypatch):
    """Helper must be a pure function — no global state mutation."""
    monkeypatch.setattr(theme, "IS_MACOS", False)
    a = theme.mac_padding((6, 3), (11, 7))
    b = theme.mac_padding((6, 3), (11, 7))
    assert a == b == (6, 3)


def test_macos_widget_pad_returns_dict_on_macos(monkeypatch):
    monkeypatch.setattr(theme, "IS_MACOS", True)
    pad = theme.macos_widget_pad()
    assert isinstance(pad, dict)
    assert "padx" in pad and "pady" in pad
    assert pad["padx"] > 0 and pad["pady"] > 0


def test_macos_widget_pad_returns_empty_on_other_platforms(monkeypatch):
    monkeypatch.setattr(theme, "IS_MACOS", False)
    assert theme.macos_widget_pad() == {}


def test_macos_widget_pad_spread_safe_on_non_macos(monkeypatch):
    """Spreading ``**macos_widget_pad()`` into a constructor on Windows
    must be a no-op (zero kwargs), not pass ``padx=0`` which would still
    affect rendering."""
    monkeypatch.setattr(theme, "IS_MACOS", False)
    pad = theme.macos_widget_pad()
    assert len(pad) == 0


def test_click_debug_false_when_env_unset(monkeypatch):
    """CLICK_DEBUG is captured at import time, so we re-import the module
    after unsetting the env var to verify the off-path."""
    monkeypatch.delenv("KLING_DEBUG_CLICKS", raising=False)
    reloaded = importlib.reload(theme)
    assert reloaded.CLICK_DEBUG is False


def test_click_debug_true_when_env_set(monkeypatch):
    monkeypatch.setenv("KLING_DEBUG_CLICKS", "1")
    reloaded = importlib.reload(theme)
    try:
        assert reloaded.CLICK_DEBUG is True
    finally:
        # Restore default state so other tests aren't affected.
        monkeypatch.delenv("KLING_DEBUG_CLICKS", raising=False)
        importlib.reload(theme)


def test_click_debug_only_matches_literal_one(monkeypatch):
    """``KLING_DEBUG_CLICKS=true`` should NOT enable the diagnostic — the
    contract is strictly the literal string "1" so the helper is unambiguous
    in shell scripts and CI invocations."""
    monkeypatch.setenv("KLING_DEBUG_CLICKS", "true")
    reloaded = importlib.reload(theme)
    try:
        assert reloaded.CLICK_DEBUG is False
    finally:
        monkeypatch.delenv("KLING_DEBUG_CLICKS", raising=False)
        importlib.reload(theme)


def test_attach_click_diagnostics_noop_when_disabled(monkeypatch):
    """When CLICK_DEBUG is False, attach_click_diagnostics must NOT bind
    anything — we verify by passing a dummy object that would raise
    AttributeError on .bind() if it were called."""
    monkeypatch.setattr(theme, "CLICK_DEBUG", False)

    class DummyWidget:
        def bind(self, *args, **kwargs):
            raise AssertionError("bind() should not be called when CLICK_DEBUG is off")

    theme.attach_click_diagnostics(DummyWidget(), label="test")  # must not raise


def test_attach_click_diagnostics_binds_when_enabled(monkeypatch):
    monkeypatch.setattr(theme, "CLICK_DEBUG", True)
    bindings = []

    class DummyWidget:
        def bind(self, sequence, callback, add=None):
            bindings.append((sequence, callable(callback), add))

    theme.attach_click_diagnostics(DummyWidget(), label="Expand Image")
    sequences = [s for s, _, _ in bindings]
    assert "<ButtonPress-1>" in sequences
    assert "<ButtonRelease-1>" in sequences
    # All add modes should be "+" so we never clobber existing bindings.
    for _, _, add in bindings:
        assert add == "+"
