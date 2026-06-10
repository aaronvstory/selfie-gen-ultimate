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
    (case_dir / "gen-images" / "selfie_sim81_001.png").write_bytes(b"x")
    (case_dir / "gen-videos").mkdir()
    (case_dir / "gen-videos" / "clip.mp4").write_bytes(b"x")

    found = detect_existing_outputs(case_dir)
    assert found.front_expanded is not None
    assert found.extracted is not None
    assert found.video_candidate is not None
    assert found.selfie_candidate is not None


def test_existing_output_detection_avoids_sim_substring_false_positive(tmp_path: Path):
    case_dir = tmp_path / "case2"
    case_dir.mkdir()
    (case_dir / "simple.png").write_bytes(b"x")
    (case_dir / "simone.jpg").write_bytes(b"x")
    (case_dir / "portrait_sim_001.png").write_bytes(b"x")

    found = detect_existing_outputs(case_dir)
    assert found.selfie_candidate is not None
    assert found.selfie_candidate.name == "portrait_sim_001.png"


def test_existing_output_detection_matches_scored_sim_token(tmp_path: Path):
    """The REAL generated-selfie naming is ``..._sim{NN}_001.png`` (score
    embedded). The bare-token-only pattern missed it, so existing selfies
    were silently regenerated — a paid API call — on every rerun (found
    live in E2E round 0, 2026-06-11)."""
    case_dir = tmp_path / "case-scored"
    (case_dir / "gen-images").mkdir(parents=True)
    (case_dir / "gen-images" / "extracted_nano-banana-2-edit_sim88_001.png").write_bytes(b"x")

    found = detect_existing_outputs(case_dir)
    assert found.selfie_candidate is not None
    assert found.selfie_candidate.name == "extracted_nano-banana-2-edit_sim88_001.png"


def test_existing_output_detection_never_picks_expanded_artifact(tmp_path: Path):
    """Step-5 expansion outputs (``...-expanded.png``) must NOT win the
    selfie-candidate ranking — reusing one makes Step 5 re-expand it into
    ``...-expanded-expanded.png`` (wasted paid outpaint + wrong geometry;
    found live in E2E round 1b, 2026-06-11). The raw selfie wins even when
    the expanded file is newer."""
    import os
    import time

    case_dir = tmp_path / "case-exp"
    gen_images = case_dir / "gen-images"
    gen_images.mkdir(parents=True)
    raw = gen_images / "extracted_nano-banana-2-edit_sim88_001.png"
    raw.write_bytes(b"x")
    expanded = gen_images / "extracted_nano-banana-2-edit_sim88_001-expanded.png"
    expanded.write_bytes(b"x")
    now = time.time()
    os.utime(raw, (now - 100, now - 100))
    os.utime(expanded, (now, now))  # expanded is NEWER — would win on mtime

    found = detect_existing_outputs(case_dir)
    assert found.selfie_candidate is not None
    assert found.selfie_candidate.name == "extracted_nano-banana-2-edit_sim88_001.png"


def test_existing_output_detection_ignores_unrelated_root_mp4(tmp_path: Path):
    case_dir = tmp_path / "case3"
    case_dir.mkdir()
    (case_dir / "reference_clip.mp4").write_bytes(b"x")
    (case_dir / "gen-videos").mkdir()
    (case_dir / "gen-videos" / "my_kling_video.mp4").write_bytes(b"x")

    found = detect_existing_outputs(case_dir)
    assert found.video_candidate is not None
    assert found.video_candidate.name == "my_kling_video.mp4"


def test_existing_output_detection_ignores_oldcam_outputs_as_primary(tmp_path: Path):
    case_dir = tmp_path / "case4"
    case_dir.mkdir()
    (case_dir / "gen-videos").mkdir()
    (case_dir / "gen-videos" / "clip-oldcam-v8.mp4").write_bytes(b"x")
    (case_dir / "gen-videos" / "clip_kling.mp4").write_bytes(b"x")

    found = detect_existing_outputs(case_dir)
    assert found.video_candidate is not None
    assert found.video_candidate.name == "clip_kling.mp4"


def test_existing_output_detection_prefers_newest_gen_videos_candidate(tmp_path: Path):
    case_dir = tmp_path / "case4b"
    case_dir.mkdir()
    (case_dir / "gen-videos").mkdir()
    older = case_dir / "gen-videos" / "a_kling_video.mp4"
    newer = case_dir / "gen-videos" / "b_kling_video.mp4"
    older.write_bytes(b"x")
    newer.write_bytes(b"x")
    older.touch()
    newer.touch()

    found = detect_existing_outputs(case_dir)
    assert found.video_candidate is not None
    assert found.video_candidate.name == "b_kling_video.mp4"


def test_existing_output_detection_prefers_generated_or_newer_selfie(tmp_path: Path):
    case_dir = tmp_path / "case5"
    case_dir.mkdir()
    (case_dir / "gen-images").mkdir()
    older = case_dir / "selfie_bad_old.png"
    newer = case_dir / "gen-images" / "selfie_good_new.png"
    older.write_bytes(b"x")
    newer.write_bytes(b"x")
    older.touch()
    newer.touch()

    found = detect_existing_outputs(case_dir)
    assert found.selfie_candidate is not None
    assert found.selfie_candidate.name == "selfie_good_new.png"
