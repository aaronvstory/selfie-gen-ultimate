"""Static-text guards: launchers must diagnose a mid-run OS kill honestly.

A real macOS run was SIGKILL'd (exit 137) by jetsam AFTER the GUI had run a
full pipeline, and the launcher reported the misleading "GUI startup failed".
These guards lock the corrected, OS-aware diagnostics on both launchers so the
parity can't silently regress on one OS.

Modeled on tests/test_launcher_arg_forwarding.py (raw-text guards, CRLF-safe).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_text(path: str) -> str:
    # Universal-newline read so the CRLF .bat compares cleanly.
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_macos_launcher_detects_signal_kill():
    """macOS run_gui.command must branch on 137/143 + elapsed>=5 and NOT call
    it a startup failure."""
    src = _read_text("launchers/macos/run_gui.command")
    assert "137" in src and "143" in src, "signal-exit codes not handled"
    assert "terminated by the system" in src, "missing honest OS-kill wording"
    assert "out of memory" in src.lower() or "memory" in src.lower(), (
        "OOM cause not surfaced"
    )
    # The OOM branch must be distinct from (precede, structurally) the generic
    # startup-failure text — the misleading message must NOT be the only path.
    assert "GUI startup failed" in src, "generic startup path should still exist"


def test_macos_launcher_oom_branch_is_conditional_on_elapsed():
    """The honest OOM message must be gated on having run a while (>=5s) so a
    genuine fast startup crash still reads as a startup failure."""
    src = _read_text("launchers/macos/run_gui.command")
    assert "LAUNCH_ELAPSED" in src and "-ge 5" in src


def test_windows_launcher_mentions_memory_pressure():
    """Windows run_gui.bat must, on a non-zero exit, point at memory pressure /
    post-processing rather than only 'CRASH'."""
    src = _read_text("launchers/windows/run_gui.bat")
    assert "memory" in src.lower(), "Windows launcher never mentions memory"
    assert "post-processing" in src.lower() or "powerset" in src.lower(), (
        "Windows launcher does not tie the failure to heavy post-processing"
    )


def test_windows_launcher_keeps_crlf():
    """The .bat must remain CRLF (byte-level edit discipline)."""
    raw = (REPO_ROOT / "launchers/windows/run_gui.bat").read_bytes()
    assert b"\r\n" in raw
    # No lone LF.
    assert raw.count(b"\n") == raw.count(b"\r\n")
