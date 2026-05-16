from __future__ import annotations

import os
from pathlib import Path

import pytest

from src import client


def test_video_exts_includes_m4v_unlike_detectpy():
    assert ".m4v" in client.VIDEO_EXTS
    assert {".mp4", ".mov", ".avi", ".mkv", ".webm"} <= client.VIDEO_EXTS


def test_auth_header():
    assert client.auth_header("abc") == {"Authorization": "Bearer abc"}


def test_flatten_video_children_walks_nested_tree():
    children = [
        {
            "type": "frame",
            "conclusion": "authentic",
            "score": 0.1,
            "certainty": 0.9,
            "children": [
                {
                    "type": "region",
                    "conclusion": "authentic",
                    "score": 0.05,
                    "certainty": 0.8,
                }
            ],
        },
        {"type": "frame", "conclusion": "fake", "score": 0.9, "certainty": 0.7},
    ]
    flat = client._flatten_video_children(children)
    assert len(flat) == 3
    assert all(set(n) == {"type", "conclusion", "score", "certainty"} for n in flat)
    assert {n["type"] for n in flat} == {"frame", "region"}


def test_flatten_video_children_non_list_is_empty():
    assert client._flatten_video_children(None) == []
    assert client._flatten_video_children("nope") == []


def test_trim_response_video_shape_matches_detectpy():
    raw = {
        "success": True,
        "item": {
            "created_at": "2026-05-16T00:00:00Z",
            "media_type": "video",
            "filename": "clip.mp4",
            "intelligence": {"description": "looks real", "noise": 1},
            "metrics": {"whatever": True},
            "video_metrics": {
                "score": 0.12,
                "certainty": 0.93,
                "children": [
                    {"type": "frame", "conclusion": "ok", "score": 0.1, "certainty": 0.9}
                ],
                "extra": "dropped",
            },
        },
    }
    trimmed = client.trim_response(raw)
    assert trimmed["success"] is True
    item = trimmed["item"]
    assert item["created_at"] == "2026-05-16T00:00:00Z"
    assert item["media_type"] == "video"
    assert item["filename"] == "clip.mp4"
    # Intelligence trimmed to description only.
    assert item["intelligence"] == {"description": "looks real"}
    vm = item["video_metrics"]
    assert vm["score"] == 0.12
    assert vm["certainty"] == 0.93
    assert len(vm["children"]) == 1
    assert "extra" not in vm
    assert item["metrics"] == {"whatever": True}


def test_trim_response_image_shape():
    raw = {
        "success": True,
        "item": {
            "created_at": "t",
            "media_type": "image",
            "filename": "f.png",
            "intelligence": None,
            "image_metrics": {"image": "data", "score": 0.4, "extra": 1},
        },
    }
    trimmed = client.trim_response(raw)
    assert trimmed["item"]["image_metrics"] == {"image": "data", "score": 0.4}
    assert trimmed["item"]["intelligence"] is None


def test_detect_video_raises_on_success_false(monkeypatch):
    monkeypatch.setattr(client, "submit_detect", lambda p, k: {"success": False})
    with pytest.raises(RuntimeError, match="success=false"):
        client.detect_video(Path("x.mp4"), "key")


def test_detect_video_returns_trimmed(monkeypatch):
    raw = {
        "success": True,
        "item": {
            "media_type": "video",
            "filename": "x.mp4",
            "video_metrics": {"score": 0.2, "certainty": 0.8, "children": []},
        },
    }
    monkeypatch.setattr(client, "submit_detect", lambda p, k: raw)
    out = client.detect_video(Path("x.mp4"), "key")
    assert out["item"]["video_metrics"]["score"] == 0.2


def test_resolve_api_key_from_env(monkeypatch):
    monkeypatch.setenv("RESEMBLE_API_KEY", "env-key-123")
    assert client.resolve_api_key() == "env-key-123"


def test_resolve_api_key_missing_raises_clean_message(monkeypatch):
    monkeypatch.delenv("RESEMBLE_API_KEY", raising=False)
    # Neutralize file-based env sources so the test is hermetic.
    monkeypatch.setattr(client, "_apply_env_file", lambda path: None)
    with pytest.raises(RuntimeError) as exc:
        client.resolve_api_key()
    msg = str(exc.value)
    assert "RESEMBLE_API_KEY is not set" in msg
    assert "resemble-score/.env" in msg
    assert "environment variable" in msg


def test_apply_env_file_setdefault_does_not_override(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEMBLE_API_KEY", "already-set")
    env = tmp_path / ".env"
    env.write_text('RESEMBLE_API_KEY="from-file"\n# comment\nNOPE\n')
    client._apply_env_file(env)
    assert os.environ["RESEMBLE_API_KEY"] == "already-set"


def test_apply_env_file_missing_is_noop(tmp_path):
    client._apply_env_file(tmp_path / "does-not-exist.env")  # no raise
