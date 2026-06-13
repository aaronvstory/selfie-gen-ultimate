"""Tests for automation/video_loop.py (formerly kling_gui/video_looper.py —
moved 2026-06-11 so the headless automation pipeline can loop without
importing the tkinter-laden kling_gui package; the old module remains as a
re-export shim for GUI call sites).

Covers:
- FFmpeg command structure for the visually-lossless idiom (-profile:v high
  -crf 12): perceptually identical to source, plays everywhere, ~1-1.6x size.
  (True-lossless -qp 0/-crf 0 was reverted: 5-10x bloat + unplayable output;
  Oldcam re-encodes at CRF 14 downstream so lossless buys nothing.)
- _summarize_ffmpeg_error: priority-ordered friendly one-liner for the panel.
- Failure path: friendly message goes to panel ("error"), full stderr to file ("debug").
- The kling_gui.video_looper shim re-exports the same callables.
"""
from pathlib import Path
from unittest import mock

from automation.video_loop import (
    _summarize_ffmpeg_error,
    create_looped_video,
)


def test_kling_gui_shim_reexports_same_objects():
    """GUI call sites import from kling_gui.video_looper; the shim must
    re-export the SAME callables (identity, not copies) so patching either
    namespace stays predictable."""
    import automation.video_loop as impl
    import kling_gui.video_looper as shim

    assert shim.create_looped_video is impl.create_looped_video
    assert shim.check_ffmpeg_available is impl.check_ffmpeg_available
    assert shim.get_video_duration is impl.get_video_duration


# ---------------------------------------------------------------------------
# _summarize_ffmpeg_error
# ---------------------------------------------------------------------------


def test_summarize_empty_stderr_returns_generic_message():
    assert _summarize_ffmpeg_error("") == (
        "FFmpeg returned no output (encoder may have failed to start)"
    )


def test_summarize_detects_libx264_init_failure():
    stderr = (
        "[enc:libx264 @ 0000021ad1235100] Could not open encoder before EOF\n"
        "[vost#0:0/libx264 @ 0000021ad32100c0] Task finished with error code: -22\n"
    )
    msg = _summarize_ffmpeg_error(stderr)
    assert "libx264 init failed" in msg
    assert "Could not open" not in msg  # Friendly version, not raw stderr


def test_summarize_detects_invalid_argument():
    # Only "invalid argument" present, no "could not open encoder"
    stderr = "[vost#0:0] Task finished with error code: -22 (Invalid argument)"
    msg = _summarize_ffmpeg_error(stderr)
    assert "invalid argument" in msg.lower()


def test_summarize_detects_missing_input():
    stderr = "input.mp4: No such file or directory"
    msg = _summarize_ffmpeg_error(stderr)
    assert "could not find" in msg.lower()


def test_summarize_detects_permission_denied():
    stderr = "output.mp4: Permission denied"
    msg = _summarize_ffmpeg_error(stderr)
    assert "permission denied" in msg.lower()


def test_summarize_falls_back_to_first_nonempty_line():
    stderr = "\n\nSome weird custom error from a future ffmpeg build\n\nmore text"
    msg = _summarize_ffmpeg_error(stderr)
    assert msg.startswith("Some weird custom error")


def test_summarize_truncates_very_long_first_line():
    stderr = "x" * 500
    msg = _summarize_ffmpeg_error(stderr)
    assert len(msg) <= 161  # 160 chars + ellipsis
    assert msg.endswith("…")


# ---------------------------------------------------------------------------
# FFmpeg command structure
# ---------------------------------------------------------------------------


def _captured_cmd(tmp_path: Path) -> list:
    """Run create_looped_video with a mocked subprocess.run and capture the cmd."""
    input_file = tmp_path / "clip.mp4"
    input_file.write_bytes(b"fake video bytes")
    output_file = tmp_path / "clip_looped.mp4"

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Simulate a successful encode so the function returns the output path.
        output_file.write_bytes(b"fake encoded output")
        return mock.MagicMock(returncode=0, stdout="", stderr="")

    with mock.patch(
        "automation.video_loop.check_ffmpeg_available",
        return_value=(True, "ffmpeg 6.0"),
    ), mock.patch("automation.video_loop.subprocess.run", side_effect=fake_run):
        result = create_looped_video(str(input_file), str(output_file), overwrite=True)

    assert result == str(output_file)
    return captured["cmd"]


def test_looper_cmd_uses_crf12_visually_lossless(tmp_path):
    """The cmd must use -crf 12 (visually lossless), NOT -qp 0/-crf 0.

    True-lossless (-qp 0 / -crf 0) bloats AI-generated loops 5-10x and makes
    the Loop-without-Oldcam output unplayable. CRF 12 is perceptually
    identical to the source and Oldcam re-encodes at CRF 14 downstream
    anyway, so lossless buys zero final quality.
    """
    cmd = _captured_cmd(tmp_path)
    assert "-crf" in cmd, f"Missing -crf flag in cmd: {cmd}"
    crf_value = cmd[cmd.index("-crf") + 1]
    assert crf_value == "12", f"Expected -crf 12, got -crf {crf_value}"
    assert "-qp" not in cmd, "Cmd must use -crf 12, not -qp 0 (true-lossless bloat)"


def test_looper_cmd_uses_profile_v_high(tmp_path):
    """-profile:v high is safe + required with CRF 12.

    The v1.7 "Could not open encoder" crash was specific to *true-lossless*
    (-crf 0 / -qp 0), which needs the High 4:4:4 Predictive profile while we
    force yuv420p downstream. CRF 12 is lossy-but-imperceptible, so plain
    High + yuv420p is the proven crash-free combo used for years.
    """
    cmd = _captured_cmd(tmp_path)
    assert "-profile:v" in cmd, f"Missing -profile:v flag in cmd: {cmd}"
    assert cmd[cmd.index("-profile:v") + 1] == "high"


def test_looper_cmd_uses_preset_slow(tmp_path):
    """-preset slow trades encode time for compression efficiency.

    A future regression to a faster preset (e.g. medium/veryfast) would
    enlarge the loop and partially undo this fix's size reduction, so the
    preset is pinned alongside -crf 12.
    """
    cmd = _captured_cmd(tmp_path)
    assert "-preset" in cmd, f"Missing -preset flag in cmd: {cmd}"
    assert cmd[cmd.index("-preset") + 1] == "slow"


def test_looper_cmd_omits_tune_flag(tmp_path):
    """-tune film's psy-rd settings add no benefit here; keep the cmd minimal."""
    cmd = _captured_cmd(tmp_path)
    assert "-tune" not in cmd, "Cmd must not specify -tune"


def test_looper_cmd_preserves_yuv420p_pix_fmt(tmp_path):
    """yuv420p is required for downstream OpenCV decode compatibility."""
    cmd = _captured_cmd(tmp_path)
    assert "-pix_fmt" in cmd
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"


def test_looper_cmd_uses_reverse_concat_filter(tmp_path):
    """Filter must produce forward + reverse concat for ping-pong loop."""
    cmd = _captured_cmd(tmp_path)
    assert "-filter_complex" in cmd
    filter_str = cmd[cmd.index("-filter_complex") + 1]
    assert "reverse" in filter_str
    assert "concat=n=2" in filter_str


# ---------------------------------------------------------------------------
# Failure path: friendly panel msg + full stderr to file ("debug")
# ---------------------------------------------------------------------------


def test_looper_failure_emits_friendly_error_and_debug_dump(tmp_path):
    """On FFmpeg failure, the panel sees one friendly line; the full stderr
    blob goes to the file logger under "debug" level (never the panel)."""
    input_file = tmp_path / "clip.mp4"
    input_file.write_bytes(b"fake")
    output_file = tmp_path / "clip_looped.mp4"

    fake_stderr = (
        "[enc:libx264 @ 0000021ad1235100] Could not open encoder before EOF\n"
        "[vost#0:0/libx264 @ 0000021ad32100c0] Task finished with error code: -22 (Invalid argument)\n"
        "Conversion failed!\n"
    )

    captured_logs: list = []

    def log_callback(msg: str, level: str = "info"):
        captured_logs.append((msg, level))

    with mock.patch(
        "automation.video_loop.check_ffmpeg_available",
        return_value=(True, "ffmpeg 6.0"),
    ), mock.patch(
        "automation.video_loop.subprocess.run",
        return_value=mock.MagicMock(returncode=1, stdout="", stderr=fake_stderr),
    ):
        result = create_looped_video(
            str(input_file),
            str(output_file),
            overwrite=True,
            log_callback=log_callback,
        )

    assert result is None

    # The full multi-line stderr blob is emitted under "debug" (file only).
    debug_msgs = [m for m, lvl in captured_logs if lvl == "debug"]
    assert any("Could not open encoder before EOF" in m for m in debug_msgs), (
        f"Full FFmpeg stderr must be emitted under 'debug' level. Got: {captured_logs}"
    )

    # The panel-facing message is a single friendly line under "error".
    error_msgs = [m for m, lvl in captured_logs if lvl == "error"]
    assert len(error_msgs) == 1, f"Expected exactly one 'error' message, got: {error_msgs}"
    assert "Loop encode failed:" in error_msgs[0]
    assert "libx264 init failed" in error_msgs[0]

    # The raw multi-line dump must NOT appear in any non-debug log.
    for msg, lvl in captured_logs:
        if lvl != "debug":
            assert "vost#0:0" not in msg, (
                f"Raw FFmpeg internals leaked into non-debug log: ({lvl}) {msg}"
            )
            assert "[enc:libx264" not in msg
