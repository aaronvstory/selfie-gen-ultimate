from pathlib import Path

from automation.discovery import detect_existing_outputs, discover_case_folders


def test_discovery_finds_front_and_stable_sort(tmp_path: Path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "b" / "front.jpg").write_bytes(b"x")
    (tmp_path / "a" / "front.png").write_bytes(b"x")

    records = discover_case_folders(tmp_path, ["front.png", "front.jpg", "front.jpeg"])
    assert [record.relative_key for record in records] == ["a", "b"]


def test_discovery_ignores_generated_and_runtime_dirs(tmp_path: Path):
    (tmp_path / "gen-images").mkdir()
    (tmp_path / "gen-images" / "nested").mkdir()
    (tmp_path / "gen-images" / "nested" / "front.png").write_bytes(b"x")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "front.jpeg").write_bytes(b"x")

    records = discover_case_folders(tmp_path, ["front.png", "front.jpg", "front.jpeg"])
    assert len(records) == 1
    assert records[0].relative_key == "real"


def test_existing_output_detection_scans_only_expected_dirs(tmp_path: Path):
    case_dir = tmp_path / "case1"
    case_dir.mkdir()
    (case_dir / "front-expanded.png").write_bytes(b"x")
    (case_dir / "gen-images").mkdir()
    (case_dir / "gen-images" / "extracted.png").write_bytes(b"x")
    (case_dir / "gen-videos").mkdir()
    (case_dir / "gen-videos" / "clip.mp4").write_bytes(b"x")
    (case_dir / "gen-videos" / "selfie_sim81_001.png").write_bytes(b"x")

    found = detect_existing_outputs(case_dir)
    assert found.front_expanded is not None
    assert found.extracted is not None
    assert found.video_candidate is not None
    assert found.selfie_candidate is not None

