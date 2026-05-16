"""Sequential scoring orchestration + ranking + report writers.

Score semantics (Resemble ``video_metrics.score``, 0-1): higher = more likely
deepfake, so **lower is better** for our use case (the video fooled the
detector / looks authentic). Ranking is ascending; rank 1 is the winner.
"""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

import requests

from . import client
from .discovery import VideoItem

# Operational failures we expect per-item and record (continue-on-error),
# rather than letting a bare `except Exception` also swallow real defects.
_SCORE_ERRORS = (
    requests.exceptions.RequestException,
    json.JSONDecodeError,
    RuntimeError,
    OSError,
)

REPORT_JSON_NAME = "resemble_results.json"
REPORT_CSV_NAME = "resemble_results.csv"


@dataclass
class Result:
    """One scored (or failed) video."""

    name: str
    group: str
    path: Path
    score: Optional[float] = None
    certainty: Optional[float] = None
    status: str = "pending"  # "ok" | "error" | "cancelled"
    error: str = ""
    json_path: Optional[Path] = None
    rank: Optional[int] = field(default=None)

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.score is not None


# (done_count, total, latest_result) -> None
ProgressCb = Callable[[int, int, Result], None]


def _sort_key(r: Result) -> tuple:
    """Ascending by score; errored/None sink to the bottom, stable by name."""
    if r.ok and r.score is not None:
        return (0, r.score, r.name.lower())
    return (1, float("inf"), r.name.lower())


def rank(results: Sequence[Result]) -> list[Result]:
    """Return results ordered best→worst with ``rank`` assigned (1 = winner).

    Only successfully-scored results get a numeric rank; errored ones keep
    ``rank=None`` and sort last.
    """
    ordered = sorted(results, key=_sort_key)
    position = 0
    for r in ordered:
        if r.ok:
            position += 1
            r.rank = position
        else:
            r.rank = None
    return ordered


def score_items(
    items: Sequence[VideoItem],
    api_key: str,
    *,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[Result]:
    """Score ``items`` one at a time (sequential — safest for rate limits).

    For each item: submit to Resemble, write ``<video>.json`` next to it
    (the trimmed shape detect.py persists), and invoke ``progress_cb`` with
    the running done-count and the just-finished :class:`Result`.

    ``cancel_event`` is checked before each item; once set, the remaining
    items are recorded as ``status="cancelled"`` (so partial output is still
    rankable/writable) and progress is still reported for them.
    """
    total = len(items)
    results: list[Result] = []

    for index, item in enumerate(items):
        result = Result(name=item.name, group=item.group, path=item.path)

        if cancel_event is not None and cancel_event.is_set():
            result.status = "cancelled"
            result.error = "cancelled before start"
        else:
            try:
                trimmed = client.detect_video(item.path, api_key)
                vm = (trimmed.get("item") or {}).get("video_metrics") or {}
                result.score = vm.get("score")
                result.certainty = vm.get("certainty")
                out_path = item.path.with_suffix(".json")
                out_path.write_text(
                    json.dumps(trimmed, indent=2), encoding="utf-8"
                )
                result.json_path = out_path
                if result.score is None:
                    result.status = "error"
                    result.error = "no video_metrics.score in response"
                else:
                    result.status = "ok"
            except KeyboardInterrupt:
                # Preserve everything scored so far: record this item as
                # cancelled, stop, and return the partial results so the
                # caller can still rank + write a report.
                result.status = "cancelled"
                result.error = "interrupted"
                results.append(result)
                if progress_cb is not None:
                    progress_cb(index + 1, total, result)
                if cancel_event is not None:
                    cancel_event.set()
                break
            except _SCORE_ERRORS as exc:  # expected operational failure
                result.status = "error"
                result.error = str(exc)

        results.append(result)
        if progress_cb is not None:
            progress_cb(index + 1, total, result)

    return results


def write_reports(root: Path, results: Sequence[Result]) -> tuple[Path, Path]:
    """Write the combined ``resemble_results.json`` + ``.csv`` into ``root``.

    Returns the two written paths. Results are ranked (ascending score) before
    writing so both files share the winner-first ordering.
    """
    root = Path(root)
    ordered = rank(results)
    winner = next((r for r in ordered if r.ok), None)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    json_path = root / REPORT_JSON_NAME
    payload = {
        "root": str(root),
        "generated_at": generated_at,
        "score_semantics": "lower is better (0-1 deepfake probability)",
        "winner": (
            {
                "filename": winner.name,
                "group": winner.group,
                "score": winner.score,
                "certainty": winner.certainty,
            }
            if winner
            else None
        ),
        "results": [
            {
                "rank": r.rank,
                "filename": r.name,
                "group": r.group,
                "score": r.score,
                "certainty": r.certainty,
                "status": r.status,
                "error": r.error,
            }
            for r in ordered
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    csv_path = root / REPORT_CSV_NAME
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["rank", "filename", "group", "score", "certainty", "status"]
        )
        for r in ordered:
            writer.writerow(
                [
                    "" if r.rank is None else r.rank,
                    r.name,
                    r.group,
                    "" if r.score is None else r.score,
                    "" if r.certainty is None else r.certainty,
                    r.status,
                ]
            )

    return json_path, csv_path
