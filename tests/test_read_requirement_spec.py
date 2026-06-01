"""Unit tests for scripts/read_requirement_spec.py — the comment-safe
requirement-line parser added in v2.13.

Regression context: rPPG/run_rppg.bat used to extract the mediapipe spec from
requirements.txt with `findstr /R "^[ ]*mediapipe"`. Inside the batch for/f
backtick context the anchor carets got mangled, so it matched the FIRST
"mediapipe" line — a COMMENT (`# mediapipe (matplotlib ...); pin both ...`) —
and pip choked on the `;` (InvalidMarker), failing rPPG. The parser must skip
comment lines and return only the real requirement.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "read_requirement_spec.py"


def _load():
    spec = importlib.util.spec_from_file_location("read_requirement_spec", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find(tmp_path, content: str, pkg: str = "mediapipe", fallback: str = "mediapipe==0.10.35") -> str:
    mod = _load()
    f = tmp_path / "requirements.txt"
    f.write_text(content, encoding="utf-8")
    return mod.find_spec(pkg, str(f), fallback)


def test_script_exists():
    assert SCRIPT.is_file(), "scripts/read_requirement_spec.py must exist"


def test_skips_comment_picks_real_line(tmp_path):
    """The exact v2.13 bug: a comment mentioning mediapipe precedes the real pin."""
    content = (
        "# mediapipe (matplotlib drawing_utils); pin both explicitly so the resolution\n"
        "# is deterministic, not backtrack-dependent.\n"
        "opencv-python-headless>=4.8.1.78,<4.12\n"
        "mediapipe==0.10.35\n"
        "deepface==0.0.92\n"
    )
    assert _find(tmp_path, content) == "mediapipe==0.10.35"


def test_comment_only_returns_fallback(tmp_path):
    content = "# mediapipe is great\nnumpy<2\n"
    assert _find(tmp_path, content) == "mediapipe==0.10.35"


def test_indented_comment_skipped(tmp_path):
    content = "   # mediapipe note\nmediapipe>=0.10.35\n"
    assert _find(tmp_path, content) == "mediapipe>=0.10.35"


def test_marker_form(tmp_path):
    content = 'mediapipe; python_version < "3.13"\n'
    assert _find(tmp_path, content) == 'mediapipe; python_version < "3.13"'


def test_extra_form(tmp_path):
    content = "mediapipe[all]==0.10.35\n"
    assert _find(tmp_path, content) == "mediapipe[all]==0.10.35"


def test_inline_comment_stripped(tmp_path):
    content = "mediapipe==0.10.35  # the face landmarker dep\n"
    assert _find(tmp_path, content) == "mediapipe==0.10.35"


def test_substring_package_not_matched(tmp_path):
    """`mediapipe-foo` must NOT match the `mediapipe` query (word boundary)."""
    content = "mediapipe-foo==1.0\nnumpy<2\n"
    assert _find(tmp_path, content) == "mediapipe==0.10.35"  # falls back


def test_missing_file_returns_fallback(tmp_path):
    mod = _load()
    missing = tmp_path / "does_not_exist.txt"
    assert mod.find_spec("mediapipe", str(missing), "mediapipe==0.10.35") == "mediapipe==0.10.35"


def test_real_requirements_file_picks_mediapipe_pin():
    """Against the REAL repo requirements.txt — must be the pin, not the comment."""
    mod = _load()
    result = mod.find_spec("mediapipe", str(REPO_ROOT / "requirements.txt"), "mediapipe==0.10.35")
    assert result == "mediapipe==0.10.35", f"got {result!r}"
    assert not result.startswith("#"), "must never return a comment line"


def test_main_prints_only_spec(tmp_path, capsys):
    """main() prints EXACTLY one line (the spec) to stdout for clean for/f capture."""
    mod = _load()
    f = tmp_path / "requirements.txt"
    f.write_text("# mediapipe comment\nmediapipe==0.10.35\n", encoding="utf-8")
    rc = mod.main(["read_requirement_spec.py", "mediapipe", str(f), "mediapipe==0.10.35"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "mediapipe==0.10.35"
    assert out.count("\n") == 1, "must print exactly one line to stdout"
