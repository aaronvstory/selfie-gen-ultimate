from __future__ import annotations

import csv
import json
import threading
from pathlib import Path

from src import client, scoring
from src.discovery import classify


def _items(tmp_path, names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"fake-video-bytes")
        out.append(classify(p))
    return out


def _fake_trimmed(score, certainty=0.9):
    return {
        "success": True,
        "item": {
            "created_at": "t",
            "media_type": "video",
            "filename": "f.mp4",
            "intelligence": {"description": "d"},
            "metrics": {},
            "video_metrics": {
                "score": score,
                "certainty": certainty,
                "children": [],
            },
        },
    }


def test_score_items_writes_per_video_json_and_reports(tmp_path, monkeypatch):
    items = _items(
        tmp_path,
        ["orig_k25.mp4", "a-oldcam-v9.mp4", "b-oldcam-v14.mp4"],
    )
    scores = {
        "orig_k25.mp4": 0.70,
        "a-oldcam-v9.mp4": 0.45,
        "b-oldcam-v14.mp4": 0.12,
    }
    monkeypatch.setattr(
        client,
        "detect_video",
        lambda path, key: _fake_trimmed(scores[Path(path).name]),
    )

    seen = []
    results = scoring.score_items(
        items,
        "key",
        progress_cb=lambda d, t, r: seen.append((d, t, r.name)),
    )

    # Progress fired once per item with a running count.
    assert [s[0] for s in seen] == [1, 2, 3]
    assert all(s[1] == 3 for s in seen)

    # Per-video JSON written next to each video, trimmed shape.
    for it in items:
        jp = it.path.with_suffix(".json")
        assert jp.exists()
        data = json.loads(jp.read_text())
        assert data["item"]["media_type"] == "video"

    json_path, csv_path = scoring.write_reports(tmp_path, results)
    assert json_path.name == "resemble_results.json"
    assert csv_path.name == "resemble_results.csv"

    payload = json.loads(json_path.read_text())
    # Winner = lowest score (the v14).
    assert payload["winner"]["filename"] == "b-oldcam-v14.mp4"
    ranks = [(r["filename"], r["rank"]) for r in payload["results"]]
    assert ranks == [
        ("b-oldcam-v14.mp4", 1),
        ("a-oldcam-v9.mp4", 2),
        ("orig_k25.mp4", 3),
    ]

    rows = list(csv.reader(csv_path.read_text().splitlines()))
    assert rows[0] == [
        "rank",
        "filename",
        "group",
        "score",
        "certainty",
        "status",
    ]
    assert rows[1][1] == "b-oldcam-v14.mp4"
    assert rows[1][0] == "1"


def test_score_items_records_errors_without_aborting(tmp_path, monkeypatch):
    items = _items(tmp_path, ["good-oldcam-v9.mp4", "bad-oldcam-v14.mp4"])

    def fake(path, key):
        if "bad" in Path(path).name:
            raise RuntimeError("network boom")
        return _fake_trimmed(0.3)

    monkeypatch.setattr(client, "detect_video", fake)
    results = scoring.score_items(items, "key")

    by_name = {r.name: r for r in results}
    assert by_name["good-oldcam-v9.mp4"].status == "ok"
    assert by_name["bad-oldcam-v14.mp4"].status == "error"
    assert "network boom" in by_name["bad-oldcam-v14.mp4"].error

    ordered = scoring.rank(results)
    # Errored result sinks to the bottom with rank None.
    assert ordered[0].name == "good-oldcam-v9.mp4"
    assert ordered[0].rank == 1
    assert ordered[-1].name == "bad-oldcam-v14.mp4"
    assert ordered[-1].rank is None


def test_cancel_event_marks_remaining_cancelled(tmp_path, monkeypatch):
    items = _items(tmp_path, ["a-oldcam-v9.mp4", "b-oldcam-v14.mp4"])
    cancel = threading.Event()
    cancel.set()  # cancelled before any item starts

    called = []
    monkeypatch.setattr(
        client,
        "detect_video",
        lambda p, k: called.append(p) or _fake_trimmed(0.1),
    )

    results = scoring.score_items(items, "key", cancel_event=cancel)
    assert called == []  # API never invoked
    assert all(r.status == "cancelled" for r in results)
    # Cancelled results are still rankable (sink to bottom, no winner).
    ordered = scoring.rank(results)
    assert all(r.rank is None for r in ordered)


def test_score_with_missing_score_field_is_error(tmp_path, monkeypatch):
    items = _items(tmp_path, ["x-oldcam-v9.mp4"])
    monkeypatch.setattr(
        client,
        "detect_video",
        lambda p, k: {
            "success": True,
            "item": {"media_type": "video", "video_metrics": {}},
        },
    )
    results = scoring.score_items(items, "key")
    assert results[0].status == "error"
    assert "no video_metrics.score" in results[0].error


def test_keyboardinterrupt_preserves_scored_items(tmp_path, monkeypatch):
    """Ctrl-C mid-run keeps already-scored results and stops cleanly so the
    caller can still rank + write a report (CodeRabbit/Codex finding)."""
    items = _items(
        tmp_path,
        ["a-oldcam-v9.mp4", "b-oldcam-v14.mp4", "c_kling.mp4"],
    )

    def fake(path, key):
        if Path(path).name == "b-oldcam-v14.mp4":
            raise KeyboardInterrupt
        return _fake_trimmed(0.3)

    monkeypatch.setattr(client, "detect_video", fake)
    cancel = threading.Event()
    results = scoring.score_items(items, "key", cancel_event=cancel)

    # First item scored; second recorded as cancelled; loop stopped (no 3rd).
    assert len(results) == 2
    assert results[0].name == "a-oldcam-v9.mp4"
    assert results[0].status == "ok"
    assert results[1].name == "b-oldcam-v14.mp4"
    assert results[1].status == "cancelled"
    assert cancel.is_set()
    # Partial results are still rankable / reportable.
    json_path, _ = scoring.write_reports(tmp_path, results)
    payload = json.loads(json_path.read_text())
    assert payload["winner"]["filename"] == "a-oldcam-v9.mp4"


def test_score_items_narrowed_exceptions_still_catch_runtimeerror(
    tmp_path, monkeypatch
):
    """RuntimeError (e.g. API success=false) must remain a recorded per-item
    error after narrowing the broad `except Exception`."""
    items = _items(tmp_path, ["x-oldcam-v9.mp4"])
    monkeypatch.setattr(
        client,
        "detect_video",
        lambda p, k: (_ for _ in ()).throw(RuntimeError("api said no")),
    )
    results = scoring.score_items(items, "key")
    assert results[0].status == "error"
    assert "api said no" in results[0].error
