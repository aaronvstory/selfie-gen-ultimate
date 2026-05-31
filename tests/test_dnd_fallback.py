"""Guards for the drag-and-drop graceful-degradation fix.

A user on a fresh v2.9 install crashed at GUI startup with
``Kling UI Error: Unable to load tkdnd library``. Root cause: ``import
tkinterdnd2`` succeeds (so HAS_DND is True), but ``TkinterDnD.Tk()`` then runs
Tcl ``package require tkdnd`` which raises ``tkinter.TclError`` (wrapped as
RuntimeError) when the bundled native tkdnd binary doesn't match the user's
Tcl/Tk build. That is NOT an ImportError, so the import-time guard never caught
it and the whole GUI died.

``drop_zone.create_dnd_root`` now wraps ``TkinterDnD.Tk()`` in try/except and
falls back to a plain ``tk.Tk()`` (drag-and-drop disabled, app survives). These
tests pin that contract.

Static + light-monkeypatch only — no real Tk window needed.
"""
import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DROP_ZONE = REPO_ROOT / "kling_gui" / "drop_zone.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _create_dnd_root_source() -> str:
    """Extract the create_dnd_root function body via ast (robust vs regex)."""
    src = _read(DROP_ZONE)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "create_dnd_root":
            return ast.get_source_segment(src, node) or ""
    return ""


import contextlib


@contextlib.contextmanager
def _fresh_drop_zone():
    """Yield a FRESH copy of kling_gui.drop_zone loaded from source under a
    PRIVATE throwaway module name, leaving the shared
    ``sys.modules["kling_gui.drop_zone"]`` entry completely untouched.

    Other GUI tests in the suite stub ``tkinterdnd2`` / ``kling_gui.drop_zone``
    in ``sys.modules`` (so they can import the GUI headlessly). A plain
    ``import kling_gui.drop_zone`` here could hand back such a stub — whose
    ``create_dnd_root`` lacks our try/except. Loading the real file under a
    private name (not reloading the shared module) means we always exercise the
    real code AND never perturb the shared sys.modules entry, so later tests
    like test_gui_smoke's ``import_module('kling_gui.drop_zone')`` are unaffected
    (gemini @72: a global reload leaked a torn-down module to later imports).
    """
    import importlib.util
    import sys as _sys

    src_path = DROP_ZONE
    spec = importlib.util.spec_from_file_location(
        "_test_fresh_drop_zone", str(src_path)
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        _sys.modules.pop("_test_fresh_drop_zone", None)


def test_create_dnd_root_wraps_tkinterdnd_in_try_except():
    """create_dnd_root must guard TkinterDnD.Tk() so a tkdnd LOAD failure
    (TclError/RuntimeError, NOT ImportError) degrades to plain tk.Tk()."""
    body = _create_dnd_root_source()
    assert body, "create_dnd_root not found"
    assert "try:" in body and "TkinterDnD.Tk()" in body, (
        "create_dnd_root must call TkinterDnD.Tk() inside a try block"
    )
    # The except must NOT be limited to ImportError — the runtime failure is a
    # TclError. Accept a broad Exception or an explicit TclError/RuntimeError.
    assert re.search(r"except\s+Exception", body) or re.search(
        r"except\s*\(?[^\n]*(TclError|RuntimeError)", body
    ), "create_dnd_root's except must cover TclError/RuntimeError, not just ImportError"
    assert "return tk.Tk()" in body, (
        "create_dnd_root must fall back to a plain tk.Tk() root"
    )


def test_create_dnd_root_returns_plain_tk_on_tkdnd_failure():
    """Simulate TkinterDnD.Tk() raising (tkdnd load failure) and assert
    create_dnd_root returns a plain root instead of propagating the crash."""
    import tkinter

    original_tk_tk = tkinter.Tk
    with _fresh_drop_zone() as dz:
        try:
            dz.HAS_DND = True

            class _FakeTkinterDnD:
                @staticmethod
                def Tk():
                    raise RuntimeError("Unable to load tkdnd library.")

            dz.TkinterDnD = _FakeTkinterDnD
            sentinel = object()
            tkinter.Tk = lambda: sentinel

            result = dz.create_dnd_root()
            assert result is sentinel, "must fall back to plain tk.Tk() on tkdnd failure"
            assert dz.HAS_DND is False, (
                "create_dnd_root must set HAS_DND=False after a tkdnd load failure "
                "so drop_target_register sites no-op"
            )
        finally:
            tkinter.Tk = original_tk_tk


def test_create_dnd_root_uses_tkinterdnd_when_available():
    """Happy path: when HAS_DND and TkinterDnD.Tk() works, use it."""
    with _fresh_drop_zone() as dz:
        dz.HAS_DND = True
        sentinel = object()

        class _OkTkinterDnD:
            @staticmethod
            def Tk():
                return sentinel

        dz.TkinterDnD = _OkTkinterDnD
        assert dz.create_dnd_root() is sentinel
