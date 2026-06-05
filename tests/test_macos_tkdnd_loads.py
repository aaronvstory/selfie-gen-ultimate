"""macOS Apple Silicon tkdnd load-test (regression for the v2.23 polish round).

tkinterdnd2 0.4.4 / 0.4.4.1 ship the osx-arm64 binary as `libtcl9tkdnd2.9.5`
(linked against Tcl 9.x). The standard python.org Python 3.11 on macOS uses
the system Tcl/Tk 8.6.12 — the Tcl 8.6 ↔ 9.x stubs are incompatible, so
`TkinterDnD.Tk()` raises ``RuntimeError: Unable to load tkdnd library`` and
drag-and-drop is silently disabled across the whole GUI (carousel, drop
zones, model picker).

The two surfaces this matters on:

1. The dep declarations (root + similarity + resemble-score). The cap MUST
   keep `tkinterdnd2 <= 0.4.3` until upstream ships a Tcl 8.6 arm64 binary.
   This guard is a pure-Python source check — runs on every OS.
2. A real-import probe gated on darwin-arm64 that ACTUALLY calls
   ``TkinterDnD.Tk()`` against the venv tkinterdnd2 wheel. Without this,
   pinning could drift again silently — exactly the failure mode the
   v2.17 mediapipe deep-import probe was added to prevent (see
   tests/test_mediapipe_runtime_deps.py for the matching pattern).

Why ``tkinterdnd2 < 0.4.4`` is the right floor: 0.4.3 ships
``libtkdnd2.9.3.dylib`` (Tcl 8.6) in osx-arm64/. 0.4.4 + 0.4.4.1 switched
to a Tcl 9.x build with no 8.6 fallback. There is no Tcl-9-capable Python
distribution we currently ship against, so 0.4.4+ is unusable here.
"""

from __future__ import annotations

import platform
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Layer 1 — source-text guard. Always runs (any OS).
# ---------------------------------------------------------------------------

_TKDND_REQ_RE = re.compile(
    r"""
    (^|[^A-Za-z0-9_-])     # word boundary
    tkinterdnd2            # the package
    \s*                    # optional spaces
    (?P<spec>[^;\s'"\]]+)  # the FULL version spec — comma-separated parts
                           #   are part of one spec (e.g. ">=0.3,<0.4.4"),
                           #   so we don't break on commas. Stop only at
                           #   real list-terminators: whitespace, quotes,
                           #   semicolon, or `]`.
    """,
    re.VERBOSE,
)


def _find_specs(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for match in _TKDND_REQ_RE.finditer(line):
            spec = match.group("spec").strip()
            if spec:
                out.append(spec)
    return out


def _spec_caps_to_below_044(spec: str) -> bool:
    """True if ``spec`` forbids tkinterdnd2 >= 0.4.4 (i.e. caps at 0.4.3 or older).

    Accepts the forms we actually use in this repo:
      "<0.4.4"   "<=0.4.3"   "==0.4.3"   ">=0.3,<0.4.4"   etc.
    """
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        return False
    has_upper_cap = False
    for p in parts:
        if p.startswith("<="):
            ver = p[2:].strip()
            if ver in {"0.4.3", "0.4.2", "0.4.1", "0.4.0", "0.3.0"}:
                has_upper_cap = True
        elif p.startswith("<"):
            ver = p[1:].strip()
            # "<0.4.4" or "<0.4.3" -> capped
            if ver in {"0.4.4", "0.4.3", "0.4.2", "0.4.1", "0.4.0"}:
                has_upper_cap = True
        elif p.startswith("=="):
            ver = p[2:].strip()
            if ver in {"0.4.3", "0.4.2", "0.4.1", "0.4.0", "0.3.0"}:
                has_upper_cap = True
    return has_upper_cap


_REQUIREMENT_FILES = [
    ROOT / "requirements.txt",
    ROOT / "pyproject.toml",
    ROOT / "similarity" / "requirements.txt",
    ROOT / "resemble-score" / "requirements.txt",
]


def test_every_tkinterdnd2_declaration_caps_below_044():
    """Every place that lists tkinterdnd2 MUST cap below 0.4.4.

    Reason: 0.4.4 + 0.4.4.1 ship a Tcl 9.x osx-arm64 binary, incompatible
    with the Tcl 8.6.12 that python.org Python 3.11 ships on macOS. Without
    a cap, ``uv sync`` resolves the latest version and DnD silently breaks.
    """
    seen_any = False
    for path in _REQUIREMENT_FILES:
        specs = _find_specs(path)
        if not specs:
            continue
        seen_any = True
        for spec in specs:
            assert _spec_caps_to_below_044(spec), (
                f"{path.relative_to(ROOT)}: tkinterdnd2 spec {spec!r} does NOT "
                "cap below 0.4.4. Latest tkinterdnd2 (0.4.4+) ships only a "
                "Tcl 9.x osx-arm64 binary, which is incompatible with the "
                "Tcl 8.6 that macOS python.org Python 3.11 uses — DnD will "
                "silently fail on Apple Silicon. Cap with `<0.4.4` until "
                "upstream restores a Tcl 8.6 binary."
            )
    assert seen_any, (
        "No tkinterdnd2 declaration found in any of: "
        + ", ".join(str(p.relative_to(ROOT)) for p in _REQUIREMENT_FILES)
    )


def test_uv_lock_pins_compatible_tkinterdnd2():
    """uv.lock MUST resolve tkinterdnd2 to a Tcl-8.6 compatible version.

    The lock is the source of truth for ``uv sync`` — if it carries 0.4.4+
    the launcher's uv fast-path silently breaks DnD on macOS Apple Silicon
    even when requirements.txt + pyproject.toml are properly capped.
    """
    lock = ROOT / "uv.lock"
    if not lock.exists():
        pytest.skip("uv.lock not present")
    text = lock.read_text(encoding="utf-8")
    # Find the version line under [[package]] name = "tkinterdnd2"
    match = re.search(
        r'name = "tkinterdnd2"\s*\nversion = "([^"]+)"', text
    )
    assert match, "uv.lock does not declare a tkinterdnd2 version"
    version = match.group(1)
    parts = version.split(".")
    # Reject 0.4.4 and 0.4.4.1 (Tcl 9.x binaries); allow 0.4.3 / 0.4.2 / 0.4.1 / 0.4.0 / 0.3.0
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    is_bad = (major, minor, patch) >= (0, 4, 4)
    assert not is_bad, (
        f"uv.lock pins tkinterdnd2=={version}, which ships a Tcl 9.x "
        "osx-arm64 binary incompatible with macOS Tk 8.6. Re-resolve the "
        "lock after capping in pyproject.toml. See "
        "tests/test_macos_tkdnd_loads.py for the root cause."
    )


# ---------------------------------------------------------------------------
# Layer 2 — real-import probe on darwin-arm64. Skip elsewhere.
# ---------------------------------------------------------------------------


_is_darwin_arm64 = platform.system() == "Darwin" and platform.machine() == "arm64"


@pytest.mark.skipif(not _is_darwin_arm64, reason="darwin/arm64-only regression")
def test_tkdnd_loads_on_apple_silicon():
    """Real-import probe: TkinterDnD.Tk() MUST instantiate on Apple Silicon.

    This is the deep-symbol probe — the source-text guards above ensure the
    dep declarations are correct, but a regression could still slip through
    if a venv was provisioned before the cap landed, or if the wheel cache
    held a stale download. Catching it here keeps the rule honest.
    """
    try:
        import tkinterdnd2
    except ImportError as exc:
        pytest.fail(
            f"tkinterdnd2 not installed in the test venv ({exc!r}). "
            "Re-run setup_macos.sh / uv sync."
        )
    try:
        root = tkinterdnd2.TkinterDnD.Tk()
    except RuntimeError as exc:
        msg = str(exc)
        if "Unable to load tkdnd library" in msg:
            pytest.fail(
                "tkdnd failed to load on Apple Silicon — this is the v2.23 "
                "polish regression. tkinterdnd2 wheel bundles a Tcl 9.x "
                "osx-arm64 binary but macOS Tk is 8.6. Confirm "
                "tkinterdnd2 is pinned <0.4.4 in pyproject.toml + "
                "requirements.txt + uv.lock; re-run uv sync."
            )
        raise
    try:
        version = root.TkdndVersion
        assert version, f"TkdndVersion was empty after load: {version!r}"
    finally:
        root.destroy()
