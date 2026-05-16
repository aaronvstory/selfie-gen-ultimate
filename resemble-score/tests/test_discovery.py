from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery import (
    ORIGINAL_GROUP,
    VideoItem,
    classify,
    discover,
    group_sort_key,
)


@pytest.mark.parametrize(
    "name,expected_group,expected_version",
    [
        ("img_k25tpro_p1_1.mp4", ORIGINAL_GROUP, None),
        ("portrait_k30std_p2_2.mp4", ORIGINAL_GROUP, None),
        ("img_k25tpro_p1_1-oldcam-v14.mp4", "Oldcam v14", 14),
        ("clip-oldcam-v9.MOV", "Oldcam v9", 9),
        ("a-oldcam-v10.webm", "Oldcam v10", 10),
        ("x_looped-oldcam-v7.mp4", "Oldcam v7", 7),
        ("video-oldcam-v123.m4v", "Oldcam v123", 123),
        # "-oldcam-" without a version digit is NOT an oldcam output.
        ("weird-oldcam-final.mp4", ORIGINAL_GROUP, None),
    ],
)
def test_classify(name, expected_group, expected_version):
    item = classify(Path(name))
    assert item.group == expected_group
    assert item.version == expected_version


def test_classify_case_insensitive_extension_and_tag():
    item = classify(Path("Clip-OLDCAM-V11.MP4"))
    assert item.group == "Oldcam v11"
    assert item.version == 11


def test_group_sort_key_orders_original_first_then_oldcam_asc():
    items = [
        classify(Path("b-oldcam-v14.mp4")),
        classify(Path("a-oldcam-v9.mp4")),
        classify(Path("original_k25.mp4")),
        classify(Path("another_kling.mp4")),
        classify(Path("c-oldcam-v9.mp4")),
    ]
    ordered = sorted(items, key=group_sort_key)
    groups = [i.group for i in ordered]
    # Both originals first (alpha by name), then v9s (alpha), then v14.
    assert groups[0] == ORIGINAL_GROUP
    assert groups[1] == ORIGINAL_GROUP
    assert groups[2:] == ["Oldcam v9", "Oldcam v9", "Oldcam v14"]
    assert ordered[0].name == "another_kling.mp4"
    assert ordered[1].name == "original_k25.mp4"
    assert ordered[2].name == "a-oldcam-v9.mp4"
    assert ordered[3].name == "c-oldcam-v9.mp4"


def test_discover_non_recursive_skips_subfolders_and_nonvideos(tmp_path):
    (tmp_path / "top_kling.mp4").write_bytes(b"x")
    (tmp_path / "top-oldcam-v14.mp4").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("not a video")
    (tmp_path / "image.png").write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep-oldcam-v9.mp4").write_bytes(b"x")

    items = discover(tmp_path, recursive=False)
    names = {i.name for i in items}
    assert names == {"top_kling.mp4", "top-oldcam-v14.mp4"}


def test_discover_recursive_finds_nested(tmp_path):
    (tmp_path / "top_kling.mp4").write_bytes(b"x")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "deep-oldcam-v9.mp4").write_bytes(b"x")

    items = discover(tmp_path, recursive=True)
    names = {i.name for i in items}
    assert names == {"top_kling.mp4", "deep-oldcam-v9.mp4"}
    # Sorted: Original before Oldcam.
    assert items[0].name == "top_kling.mp4"
    assert items[1].group == "Oldcam v9"


def test_discover_rejects_non_directory(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    with pytest.raises(NotADirectoryError):
        discover(f, recursive=False)


def test_videoitem_name_property():
    it = VideoItem(path=Path("/a/b/c.mp4"), group=ORIGINAL_GROUP, version=None)
    assert it.name == "c.mp4"
