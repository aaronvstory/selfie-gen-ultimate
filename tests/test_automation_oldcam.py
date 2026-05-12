import io
from pathlib import Path
from unittest.mock import MagicMock

import automation.oldcam as oldcam


def test_discover_versions_and_output_path(tmp_path: Path):
    for name in ("oldcam-v7", "oldcam-v8"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "launcher.py").write_text("pass", encoding="utf-8")

    versions = oldcam.discover_oldcam_versions(tmp_path)
    assert versions == ["v7", "v8"]

    output = oldcam.build_oldcam_output_path(Path("clip.mp4"), "v8")
    assert output.name == "clip-oldcam-v8.mp4"


def test_run_oldcam_version_success(monkeypatch, tmp_path: Path):
    oldcam_dir = tmp_path / "oldcam-v8"
    oldcam_dir.mkdir()
    (oldcam_dir / "launcher.py").write_text("print('ok')", encoding="utf-8")
    input_video = tmp_path / "in.mp4"
    input_video.write_bytes(b"mp4")
    expected_output = oldcam.build_oldcam_output_path(input_video, "v8")

    monkeypatch.setattr(oldcam, "ensure_oldcam_dependencies", lambda: (True, None))

    def fake_popen(*args, **kwargs):
        expected_output.write_bytes(b"done")
        mock = MagicMock()
        mock.stdout = io.StringIO("ok\n")
        mock.wait.return_value = 0
        mock.poll.return_value = 0
        return mock

    monkeypatch.setattr(oldcam.subprocess, "Popen", fake_popen)

    result = oldcam.run_oldcam_version(
        video_path=input_video,
        version="v8",
        repo_root=tmp_path,
    )
    assert result == expected_output


def test_run_oldcam_version_failure(monkeypatch, tmp_path: Path):
    oldcam_dir = tmp_path / "oldcam-v8"
    oldcam_dir.mkdir()
    (oldcam_dir / "launcher.py").write_text("print('ok')", encoding="utf-8")
    input_video = tmp_path / "in.mp4"
    input_video.write_bytes(b"mp4")

    monkeypatch.setattr(oldcam, "ensure_oldcam_dependencies", lambda: (True, None))

    def fake_popen_fail(*args, **kwargs):
        mock = MagicMock()
        mock.stdout = io.StringIO("boom\n")
        mock.wait.return_value = 1
        mock.poll.return_value = 1
        return mock

    monkeypatch.setattr(oldcam.subprocess, "Popen", fake_popen_fail)
    result = oldcam.run_oldcam_version(video_path=input_video, version="v8", repo_root=tmp_path)
    assert result is None


def test_run_oldcam_version_timeout_returns_none(monkeypatch, tmp_path: Path):
    oldcam_dir = tmp_path / "oldcam-v8"
    oldcam_dir.mkdir()
    (oldcam_dir / "launcher.py").write_text("print('ok')", encoding="utf-8")
    input_video = tmp_path / "in.mp4"
    input_video.write_bytes(b"mp4")

    monkeypatch.setattr(oldcam, "ensure_oldcam_dependencies", lambda: (True, None))

    def fake_popen_timeout(*args, **kwargs):
        mock = MagicMock()
        mock.stdout = io.StringIO("")
        mock.wait.side_effect = oldcam.subprocess.TimeoutExpired(cmd="python launcher.py", timeout=5)
        mock.poll.return_value = None
        return mock

    monkeypatch.setattr(oldcam.subprocess, "Popen", fake_popen_timeout)
    result = oldcam.run_oldcam_version(video_path=input_video, version="v8", repo_root=tmp_path, timeout_seconds=5)
    assert result is None
