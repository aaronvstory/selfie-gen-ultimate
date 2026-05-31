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


def _fresh_drop_zone():
    """Return the REAL kling_gui.drop_zone module, force-reloaded from disk.

    Other GUI tests in the suite stub ``tkinterdnd2`` / ``kling_gui.drop_zone``
    in ``sys.modules`` (so they can import the GUI headlessly). A plain
    ``import kling_gui.drop_zone`` here would then hand back that stub — whose
    ``create_dnd_root`` lacks our try/except — and the behavioral test fails
    only in full-suite order. Reload from source so we always exercise the real
    code regardless of suite ordering.
    """
    import importlib
    import sys as _sys

    _sys.modules.pop("kling_gui.drop_zone", None)
    import kling_gui.drop_zone as dz  # noqa: E402
    return importlib.reload(dz)


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

    dz = _fresh_drop_zone()
    original_has_dnd = dz.HAS_DND
    original_tkinterdnd = getattr(dz, "TkinterDnD", None)
    original_tk_tk = tkinter.Tk
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
            "create_dnd_root must set HAS_DND=False after a tkdnd load failure so "
            "drop_target_register sites no-op"
        )
    finally:
        dz.HAS_DND = original_has_dnd
        if original_tkinterdnd is not None:
            dz.TkinterDnD = original_tkinterdnd
        elif hasattr(dz, "TkinterDnD"):
            del dz.TkinterDnD
        tkinter.Tk = original_tk_tk


def test_create_dnd_root_uses_tkinterdnd_when_available():
    """Happy path: when HAS_DND and TkinterDnD.Tk() works, use it."""
    dz = _fresh_drop_zone()
    original_has_dnd = dz.HAS_DND
    original_tkinterdnd = getattr(dz, "TkinterDnD", None)
    try:
        dz.HAS_DND = True
        sentinel = object()

        class _OkTkinterDnD:
            @staticmethod
            def Tk():
                return sentinel

        dz.TkinterDnD = _OkTkinterDnD
        assert dz.create_dnd_root() is sentinel
    finally:
        dz.HAS_DND = original_has_dnd
        if original_tkinterdnd is not None:
            dz.TkinterDnD = original_tkinterdnd
        elif hasattr(dz, "TkinterDnD"):
            del dz.TkinterDnD
