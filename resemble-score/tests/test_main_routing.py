"""CLI-vs-GUI routing in main.main().

Regression guard for the CodeRabbit/Codex finding: `--no-recursive` is a
CLI-only flag and must route to the CLI, not silently launch the GUI.
"""

from __future__ import annotations

import sys

import pytest

import main as app_main


def _run(argv, monkeypatch):
    """Invoke app_main.main() with argv; return ('gui'|'cli', captured)."""
    called = {}

    def fake_run_gui():
        called["mode"] = "gui"

    def fake_run_cli(**kwargs):
        called["mode"] = "cli"
        called["kwargs"] = kwargs
        return 0

    # main() imports these lazily from src.* — patch at the source modules.
    import src.gui as gui_mod
    import src.cli as cli_mod

    monkeypatch.setattr(gui_mod, "run_gui", fake_run_gui)
    monkeypatch.setattr(cli_mod, "run_cli", fake_run_cli)
    monkeypatch.setattr(sys, "argv", ["main.py", *argv])

    try:
        app_main.main()
    except SystemExit as e:
        called["exit"] = e.code
    return called.get("mode"), called


def test_no_args_launches_gui(monkeypatch):
    mode, _ = _run([], monkeypatch)
    assert mode == "gui"


def test_no_recursive_routes_to_cli(monkeypatch):
    """The bug: --no-recursive previously launched the GUI."""
    mode, called = _run(["--no-recursive"], monkeypatch)
    assert mode == "cli"
    assert called["kwargs"]["recursive"] is False


def test_recursive_alone_still_gui(monkeypatch):
    """--recursive is the default value, so it is NOT a CLI trigger by
    itself (only the explicit negation is)."""
    mode, _ = _run(["--recursive"], monkeypatch)
    assert mode == "gui"


def test_cli_flag_routes_to_cli(monkeypatch):
    mode, called = _run(["--cli"], monkeypatch)
    assert mode == "cli"
    assert called["kwargs"]["recursive"] is True


def test_folder_routes_to_cli(monkeypatch):
    mode, called = _run(["--folder", "X"], monkeypatch)
    assert mode == "cli"
    assert called["kwargs"]["folder"] == "X"


def test_select_all_routes_to_cli(monkeypatch):
    mode, called = _run(["--all"], monkeypatch)
    assert mode == "cli"
    assert called["kwargs"]["select_all"] is True
