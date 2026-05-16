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


def _fake_trimmed(frame_mean, certainty=0.9, *, with_children=True):
    """Build a realistic trimmed video result.

    ``frame_mean`` controls the per-frame children scores (the metric the
    ranking uses); the top-level score is the rounded Fake/1.0 verdict the
    real API returns, which must NOT drive ranking.
    """
    children = []
    if with_children:
        children = [
            {
                "type": "VideoResult",
                "conclusion": "Fake",
                "score": 1.0,
                "certainty": certainty,
            },
            {
                "type": "VideoChunkResult",
                "conclusion": "Fake",
                "score": frame_mean,
                "certainty": certainty,
            },
            # Two frames whose mean == frame_mean.
            {
                "type": "VideoFrameResult",
                "conclusion": "Fake",
                "score": frame_mean - 0.02,
                "certainty": certainty,
            },
            {
                "type": "VideoFrameResult",
                "conclusion": "Fake",
                "score": frame_mean + 0.02,
                "certainty": certainty,
            },
        ]
    return {
        "success": True,
        "item": {
            "created_at": "t",
            "media_type": "video",
            "filename": "f.mp4",
            "intelligence": {"description": "d"},
            "metrics": {},
            "video_metrics": {
                "score": 1.0,  # rounded verdict — must not be ranked on
                "certainty": certainty,
                "children": children,
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

    # Per-video JSON written next to each video, trimmed shape. The sidecar
    # name keeps the original extension (foo.mp4 -> foo.mp4.json) so
    # same-stem/different-ext inputs never collide.
    for it in items:
        jp = scoring.sidecar_json_path(it.path)
        assert jp.name.endswith(".mp4.json")
        assert jp.exists()
        data = json.loads(jp.read_text())
        assert data["item"]["media_type"] == "video"

    json_path, csv_path, md_path = scoring.write_reports(tmp_path, results)
    assert json_path.name == "resemble_results.json"
    assert csv_path.name == "resemble_results.csv"
    assert md_path.name == "resemble_results.md"
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    # Winner callout + the labelled metric table + explainer all present.
    assert "# resemble-score results" in md
    assert "Winner:" in md
    assert "Frame Mean" in md
    assert "## What the numbers mean" in md
    assert (
        "| Rank | Group | File | Frame Mean | Frame Min | Frame Max "
        "| Chunk Mean | Frames | Verdict | Status |" in md
    )
    assert "b-oldcam-v14.mp4" in md
    # The raw rounded verdict is shown but clearly separate from the metric.
    assert "Fake (1.0000)" in md

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
        "frame_mean",
        "frame_min",
        "frame_max",
        "chunk_mean",
        "frame_count",
        "verdict_label",
        "verdict_score",
        "certainty",
        "status",
    ]
    assert rows[1][1] == "b-oldcam-v14.mp4"
    assert rows[1][0] == "1"
    # Verdict (raw rounded) is recorded but is NOT the ranked column.
    assert rows[1][8] == "Fake"
    assert float(rows[1][9]) == 1.0
    # The ranked metric (frame_mean) reflects the lowest input.
    assert abs(float(rows[1][3]) - 0.12) < 1e-6


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


def test_no_per_frame_children_is_error(tmp_path, monkeypatch):
    """A response without per-frame children can't be compared -> error."""
    items = _items(tmp_path, ["x-oldcam-v9.mp4"])
    monkeypatch.setattr(
        client,
        "detect_video",
        lambda p, k: _fake_trimmed(0.5, with_children=False),
    )
    results = scoring.score_items(items, "key")
    assert results[0].status == "error"
    assert "no per-frame scores" in results[0].error
    assert results[0].frame_mean is None


def test_extract_metrics_aggregates_children_not_top_score():
    """The rounded top-level 1.0 verdict must NOT become the ranked score;
    frame_mean is the mean of VideoFrameResult children."""
    trimmed = _fake_trimmed(0.30)  # frames at 0.28 and 0.32 -> mean 0.30
    m = scoring.extract_metrics(trimmed)
    assert m["verdict_score"] == 1.0          # raw rounded verdict
    assert m["verdict_label"] == "Fake"
    assert abs(m["frame_mean"] - 0.30) < 1e-9  # the real differentiator
    assert abs(m["frame_min"] - 0.28) < 1e-9
    assert abs(m["frame_max"] - 0.32) < 1e-9
    assert m["frame_count"] == 2


def test_sidecar_json_no_collision_same_stem_diff_ext(tmp_path, monkeypatch):
    """clip.mp4 and clip.mov must NOT both write clip.json (Codex P2)."""
    from src.discovery import classify

    a = tmp_path / "clip.mp4"
    b = tmp_path / "clip.mov"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    items = [classify(a), classify(b)]

    scores = {"clip.mp4": 0.20, "clip.mov": 0.80}
    monkeypatch.setattr(
        client,
        "detect_video",
        lambda path, key: _fake_trimmed(scores[Path(path).name]),
    )
    results = scoring.score_items(items, "key")

    p_mp4 = scoring.sidecar_json_path(a)
    p_mov = scoring.sidecar_json_path(b)
    assert p_mp4.name == "clip.mp4.json"
    assert p_mov.name == "clip.mov.json"
    assert p_mp4 != p_mov
    assert p_mp4.exists() and p_mov.exists()
    # Each sidecar holds ITS OWN payload — no silent overwrite.
    assert json.loads(p_mp4.read_text())["item"]["filename"]
    assert all(r.status == "ok" for r in results)


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
    json_path, _, _ = scoring.write_reports(tmp_path, results)
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
