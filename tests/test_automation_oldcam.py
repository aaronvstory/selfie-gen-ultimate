from pathlib import Path
from types import SimpleNamespace

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

    def fake_run(*args, **kwargs):
        expected_output.write_bytes(b"done")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(oldcam.subprocess, "run", fake_run)

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
    monkeypatch.setattr(
        oldcam.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    result = oldcam.run_oldcam_version(video_path=input_video, version="v8", repo_root=tmp_path)
    assert result is None
