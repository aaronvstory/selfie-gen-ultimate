"""
Tests for automation/video_crush.py

Uses a tiny synthetic MP4 fixture created via FFmpeg when available.
All tests are skipped with a clear message when FFmpeg is not on PATH.
"""

import subprocess
from pathlib import Path

import pytest

from automation.video_crush import (
    check_ffmpeg_available,
    crush_video,
    _summarize_ffmpeg_error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ffmpeg_available() -> bool:
    ok, _ = check_ffmpeg_available()
    return ok


def _make_tiny_mp4(path: Path) -> Path:
    """Create a 1-second 320x240 colour-bar MP4 using FFmpeg test sources."""
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x240:d=1",
            "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            str(path),
        ],
        capture_output=True, check=True, timeout=30,
    )
    return path


# ---------------------------------------------------------------------------
# Unit tests — no FFmpeg required
# ---------------------------------------------------------------------------

class TestSummarizeFfmpegError:
    def test_empty_stderr(self):
        msg = _summarize_ffmpeg_error("")
        assert msg  # never empty

    def test_no_such_file(self):
        msg = _summarize_ffmpeg_error("no such file or directory")
        assert "file" in msg.lower()

    def test_permission_denied(self):
        msg = _summarize_ffmpeg_error("permission denied: /tmp/x.mp4")
        assert "permission" in msg.lower()

    def test_long_first_line_truncated(self):
        long_line = "x" * 200
        msg = _summarize_ffmpeg_error(long_line)
        assert len(msg) <= 165  # 160 + "…" (1 char)

    def test_multiline_returns_first_nonempty(self):
        msg = _summarize_ffmpeg_error("\n\nconversion failed\nmore stuff")
        assert "conversion" in msg.lower()


class TestCrushVideoMissingInputEdgeCases:
    def test_directory_as_input_returns_none(self, tmp_path):
        result = crush_video(str(tmp_path))
        assert result is None

    def test_directory_as_input_fires_log(self, tmp_path):
        logs = []
        crush_video(str(tmp_path), log_callback=lambda m, level: logs.append((m, level)))
        assert any("not a file" in m.lower() or "error" in level for m, level in logs)


class TestCheckFfmpegAvailable:
    def test_returns_tuple(self):
        ok, msg = check_ffmpeg_available()
        assert isinstance(ok, bool)
        assert isinstance(msg, str)
        assert msg  # never blank

    def test_consistent(self):
        r1 = check_ffmpeg_available()
        r2 = check_ffmpeg_available()
        assert r1[0] == r2[0]


class TestCrushVideoMissingInput:
    def test_nonexistent_input_returns_none(self):
        result = crush_video("/nonexistent/path/clip.mp4")
        assert result is None

    def test_nonexistent_input_fires_log(self):
        logs = []
        crush_video("/nonexistent/path/clip.mp4", log_callback=lambda m, level: logs.append((m, level)))
        assert any("not found" in m.lower() or "error" in level for m, level in logs)


# ---------------------------------------------------------------------------
# Integration tests — require FFmpeg
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tiny_mp4(tmp_path_factory):
    """Shared tiny MP4 fixture, created once per test session."""
    if not _ffmpeg_available():
        pytest.skip("FFmpeg not available — skipping integration tests")
    base = tmp_path_factory.mktemp("crush_fixture")
    mp4 = base / "source.mp4"
    _make_tiny_mp4(mp4)
    return mp4


@pytest.mark.skipif(not _ffmpeg_available(), reason="FFmpeg not in PATH")
class TestCrushVideoIntegration:
    def test_output_exists(self, tiny_mp4, tmp_path):
        out = str(tmp_path / "out_crush.mp4")
        result = crush_video(str(tiny_mp4), output_path=out)
        assert result is not None
        assert Path(result).exists()

    def test_output_is_valid_mp4(self, tiny_mp4, tmp_path):
        # Size comparison is unreliable on micro-fixtures (container overhead
        # can exceed the saved bytes). Just verify the output is a non-empty MP4.
        out = str(tmp_path / "out_crush.mp4")
        result = crush_video(str(tiny_mp4), output_path=out)
        assert result is not None
        assert Path(result).stat().st_size > 0
        # MP4 ftyp box starts at byte 4 — verify the magic bytes are present.
        with open(result, "rb") as f:
            f.seek(4)
            magic = f.read(4)
        assert magic in (b"ftyp", b"mdat", b"moov"), f"unexpected box: {magic}"

    def test_default_suffix_naming(self, tiny_mp4, tmp_path):
        # Copy fixture to tmp so the crush file lands in a writable temp dir.
        import shutil
        src = tmp_path / "myclip.mp4"
        shutil.copy2(tiny_mp4, src)
        result = crush_video(str(src))
        assert result is not None
        assert Path(result).name == "myclip_crush.mp4"

    def test_custom_suffix(self, tiny_mp4, tmp_path):
        import shutil
        src = tmp_path / "myclip2.mp4"
        shutil.copy2(tiny_mp4, src)
        result = crush_video(str(src), suffix="_480")
        assert result is not None
        assert Path(result).name == "myclip2_480.mp4"

    def test_log_callback_called(self, tiny_mp4, tmp_path):
        logs = []
        out = str(tmp_path / "logged_crush.mp4")
        crush_video(str(tiny_mp4), output_path=out, log_callback=lambda m, l: logs.append((m, l)))
        assert logs  # at least one log line emitted

    def test_overwrite_false_skips_existing(self, tiny_mp4, tmp_path):
        out = str(tmp_path / "skip_crush.mp4")
        r1 = crush_video(str(tiny_mp4), output_path=out, overwrite=True)
        assert r1 is not None
        size1 = Path(out).stat().st_size
        # Second call with overwrite=False → same path returned, file unchanged.
        r2 = crush_video(str(tiny_mp4), output_path=out, overwrite=False)
        assert r2 is not None
        assert Path(out).stat().st_size == size1

    def test_target_height_parameter(self, tiny_mp4, tmp_path):
        out = str(tmp_path / "crush_360.mp4")
        result = crush_video(str(tiny_mp4), output_path=out, target_height=360)
        assert result is not None
        assert Path(result).exists()

    def test_returns_absolute_path(self, tiny_mp4, tmp_path):
        out = str(tmp_path / "abs_crush.mp4")
        result = crush_video(str(tiny_mp4), output_path=out)
        assert result is not None
        assert Path(result).is_absolute()
