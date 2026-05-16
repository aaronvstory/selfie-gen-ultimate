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
REPORT_MD_NAME = "resemble_results.md"


@dataclass
class Result:
    """One scored (or failed) video.

    The Resemble API's top-level ``video_metrics.score`` is a coarse
    classification verdict that rounds to 1.0 ("Fake") for almost any
    AI-derived clip, so it cannot differentiate variants. The real signal
    is the per-frame / per-chunk children scores; ``frame_mean`` is the
    primary ranked metric (lower = looks more authentic to the detector).
    """

    name: str
    group: str
    path: Path
    status: str = "pending"  # "ok" | "error" | "cancelled"
    error: str = ""
    json_path: Optional[Path] = None
    rank: Optional[int] = field(default=None)

    # Raw API verdict (kept auditable, not used for ranking).
    verdict_label: str = ""           # "Fake" / "Real" / ""
    verdict_score: Optional[float] = None      # rounded top-level score
    certainty: Optional[float] = None          # top-level certainty

    # The metrics that actually differentiate videos.
    frame_mean: Optional[float] = None
    frame_min: Optional[float] = None
    frame_max: Optional[float] = None
    chunk_mean: Optional[float] = None
    frame_count: int = 0

    @property
    def ok(self) -> bool:
        # "loaded" = restored from an existing sidecar JSON; it ranks and
        # can win exactly like a freshly-scored "ok" result.
        return (
            self.status in ("ok", "loaded")
            and self.frame_mean is not None
        )

    @property
    def scored(self) -> bool:
        """True if this video already has a usable result (fresh or loaded)
        — used to skip the API for incremental runs."""
        return self.ok

    @property
    def primary(self) -> Optional[float]:
        """The metric the ranking/winner is based on (frame mean)."""
        return self.frame_mean


# (done_count, total, latest_result) -> None
ProgressCb = Callable[[int, int, Result], None]


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def sidecar_json_path(video: Path) -> Path:
    """Canonical per-video result path. ``foo.mp4`` -> ``foo.mp4.json``.

    Keeping the extension means ``clip.mp4`` and ``clip.mov`` in one folder
    don't both write ``clip.json`` and clobber each other (Codex review).
    New results are always written here.
    """
    return video.with_name(video.name + ".json")


def _legacy_sidecar_path(video: Path) -> Path:
    """Pre-collision-fix sidecar name (``foo.mp4`` -> ``foo.json``).

    Read-only fallback so results from earlier runs still auto-load when a
    folder is re-opened; new writes never use this form.
    """
    return video.with_suffix(".json")


def existing_sidecar(video: Path) -> Optional[Path]:
    """Return the sidecar to read for ``video``: the canonical name if
    present, else the legacy name, else ``None``."""
    canonical = sidecar_json_path(video)
    if canonical.is_file():
        return canonical
    legacy = _legacy_sidecar_path(video)
    if legacy != canonical and legacy.is_file():
        return legacy
    return None


def extract_metrics(trimmed: dict) -> dict:
    """Pull comparable metrics out of a trimmed Resemble video result.

    Walks ``video_metrics.children`` (already flattened by the client) and
    aggregates the per-frame / per-chunk scores, since the top-level score
    is just a rounded Fake/Real verdict.
    """
    item = (trimmed or {}).get("item") or {}
    vm = item.get("video_metrics") or {}
    children = vm.get("children") or []

    frame_scores: list[float] = []
    chunk_scores: list[float] = []
    verdict_label = ""
    for c in children:
        if not isinstance(c, dict):
            continue
        s = c.get("score")
        ctype = c.get("type")
        if ctype == "VideoFrameResult" and isinstance(s, (int, float)):
            frame_scores.append(float(s))
        elif ctype == "VideoChunkResult" and isinstance(s, (int, float)):
            chunk_scores.append(float(s))
        elif ctype == "VideoResult" and not verdict_label:
            verdict_label = str(c.get("conclusion") or "")

    return {
        "verdict_label": verdict_label,
        "verdict_score": vm.get("score"),
        "certainty": vm.get("certainty"),
        "frame_mean": _mean(frame_scores),
        "frame_min": min(frame_scores) if frame_scores else None,
        "frame_max": max(frame_scores) if frame_scores else None,
        "chunk_mean": _mean(chunk_scores),
        "frame_count": len(frame_scores),
    }


# Metrics the table can be ranked by. label -> Result attribute.
# For all of these, lower = more authentic, so ascending = best first.
SORT_METRICS: dict[str, str] = {
    "frame_mean": "frame_mean",
    "frame_min": "frame_min",
    "frame_max": "frame_max",
    "chunk_mean": "chunk_mean",
}
DEFAULT_SORT = "frame_mean"


def _metric_value(r: Result, by: str) -> Optional[float]:
    return getattr(r, SORT_METRICS.get(by, DEFAULT_SORT), None)


def _sort_key(r: Result, by: str, descending: bool) -> tuple:
    """Order by the chosen metric; errored/missing always sink to bottom."""
    v = _metric_value(r, by)
    if r.ok and v is not None:
        # Negate for descending so errored rows (group 1) still stay last.
        return (0, -v if descending else v, r.name.lower())
    return (1, float("inf"), r.name.lower())


def rank(
    results: Sequence[Result],
    *,
    by: str = DEFAULT_SORT,
    descending: bool = False,
) -> list[Result]:
    """Return results ordered best→worst with ``rank`` assigned (1 = best).

    ``by`` selects the metric (see :data:`SORT_METRICS`); default is
    ``frame_mean`` ascending (lower = more authentic). Only successfully
    scored results get a numeric rank; errored/unscored keep ``rank=None``
    and sort last regardless of direction.
    """
    if by not in SORT_METRICS:
        by = DEFAULT_SORT
    ordered = sorted(
        results, key=lambda r: _sort_key(r, by, descending)
    )
    position = 0
    for r in ordered:
        if r.ok:
            position += 1
            r.rank = position
        else:
            r.rank = None
    return ordered


def load_existing_result(video: Path, group: str) -> Optional[Result]:
    """Build a :class:`Result` from an already-written sidecar JSON.

    Reads the canonical ``foo.mp4.json`` or, for results written before the
    collision fix, the legacy ``foo.json``. Returns ``None`` if no sidecar
    exists or it can't be parsed into a comparable result (so the caller
    treats the video as un-scored and can run the API on just those). No
    network involved.
    """
    sidecar = existing_sidecar(video)
    if sidecar is None:
        return None
    try:
        trimmed = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    r = Result(name=video.name, group=group, path=video)
    r.json_path = sidecar
    try:
        m = extract_metrics(trimmed)
    except Exception:
        return None
    r.verdict_label = m["verdict_label"]
    r.verdict_score = m["verdict_score"]
    r.certainty = m["certainty"]
    r.frame_mean = m["frame_mean"]
    r.frame_min = m["frame_min"]
    r.frame_max = m["frame_max"]
    r.chunk_mean = m["chunk_mean"]
    r.frame_count = m["frame_count"]
    if r.frame_mean is None:
        return None
    r.status = "loaded"  # distinguishes "from disk" vs freshly "ok"
    return r


def score_items(
    items: Sequence[VideoItem],
    api_key: str,
    *,
    progress_cb: Optional[ProgressCb] = None,
    cancel_event: Optional[threading.Event] = None,
) -> list[Result]:
    """Score ``items`` one at a time (sequential — safest for rate limits).

    For each item: submit to Resemble, write ``<video>.<ext>.json`` next to
    it (trimmed shape; see :func:`sidecar_json_path`) and call ``progress_cb``
    with
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
                m = extract_metrics(trimmed)
                result.verdict_label = m["verdict_label"]
                result.verdict_score = m["verdict_score"]
                result.certainty = m["certainty"]
                result.frame_mean = m["frame_mean"]
                result.frame_min = m["frame_min"]
                result.frame_max = m["frame_max"]
                result.chunk_mean = m["chunk_mean"]
                result.frame_count = m["frame_count"]
                out_path = sidecar_json_path(item.path)
                out_path.write_text(
                    json.dumps(trimmed, indent=2), encoding="utf-8"
                )
                result.json_path = out_path
                if result.frame_mean is None:
                    result.status = "error"
                    result.error = (
                        "no per-frame scores in response "
                        "(cannot compare this video)"
                    )
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


def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.4f}"


def _md_cell(text: str) -> str:
    """Escape pipes/newlines so a filename can't break the MD table."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _render_markdown(
    root: Path,
    generated_at: str,
    ordered: Sequence[Result],
    winner: Optional[Result],
) -> str:
    lines: list[str] = []
    lines.append("# resemble-score results")
    lines.append("")
    podium = [r for r in ordered if r.ok][:3]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    if winner is not None:
        lines.append(
            f"🏆 **Winner: `{_md_cell(winner.name)}`** "
            f"({_md_cell(winner.group)}) — Frame Mean "
            f"**{_fmt(winner.frame_mean)}**"
        )
        lines.append("")
        lines.append("## Top 3")
        lines.append("")
        for r in podium:
            lines.append(
                f"{medals[r.rank]} **#{r.rank}** `{_md_cell(r.name)}` "
                f"— {_md_cell(r.group)} · Frame Mean "
                f"**{_fmt(r.frame_mean)}** "
                f"(min {_fmt(r.frame_min)})"
            )
    else:
        lines.append(
            "⚠️ **No video scored successfully** — see the Status column."
        )
    lines.append("")
    lines.append(f"- **Folder:** `{_md_cell(str(root))}`")
    lines.append(f"- **Generated:** {generated_at}")
    lines.append(f"- **Videos:** {len(ordered)}")
    lines.append("")
    lines.append("## What the numbers mean")
    lines.append("")
    lines.append(
        "All scores are 0–1 deepfake probability — **lower = looks more "
        "authentic** to the detector. Resemble's raw verdict almost always "
        "rounds to `1.000 / Fake` for AI-derived clips, so it can't tell "
        "variants apart; the **per-frame** aggregates below are what "
        "actually differentiate them."
    )
    lines.append("")
    lines.append(
        "- **Frame Mean** — average across all analysed frames "
        "(primary ranking metric)"
    )
    lines.append("- **Frame Min** — most authentic-looking single frame")
    lines.append("- **Frame Max** — least authentic single frame")
    lines.append("- **Chunk Mean** — average across video chunks")
    lines.append(
        "- **Verdict** — Resemble's raw label + rounded score "
        "(audit only, not ranked)"
    )
    lines.append("")
    header = (
        "| Rank | Group | File | Frame Mean | Frame Min | Frame Max "
        "| Chunk Mean | Frames | Verdict | Status |"
    )
    lines.append(header)
    lines.append(
        "| ---: | :--- | :--- | ---: | ---: | ---: | ---: | ---: "
        "| :--- | :--- |"
    )
    for r in ordered:
        rank_txt = "🏆 1" if (r.rank == 1 and r.ok) else (
            "" if r.rank is None else str(r.rank)
        )
        verdict = (
            f"{_md_cell(r.verdict_label)} ({_fmt(r.verdict_score)})"
            if r.verdict_label
            else "—"
        )
        lines.append(
            f"| {rank_txt} | {_md_cell(r.group)} | {_md_cell(r.name)} "
            f"| {_fmt(r.frame_mean)} | {_fmt(r.frame_min)} "
            f"| {_fmt(r.frame_max)} | {_fmt(r.chunk_mean)} "
            f"| {r.frame_count or '—'} | {verdict} "
            f"| {_md_cell(r.status)} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_reports(
    root: Path, results: Sequence[Result]
) -> tuple[Path, Path, Path]:
    """Write ``resemble_results.json`` + ``.csv`` + ``.md`` into ``root``.

    Returns ``(json_path, csv_path, md_path)``. Results are ranked (ascending
    score) before writing so all three files share the winner-first ordering.
    """
    root = Path(root)
    ordered = rank(results)
    winner = next((r for r in ordered if r.ok), None)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    json_path = root / REPORT_JSON_NAME
    payload = {
        "root": str(root),
        "generated_at": generated_at,
        "score_semantics": (
            "All values are 0-1 deepfake probability; lower = more "
            "authentic. Ranked by frame_mean. The raw Resemble verdict "
            "(verdict_label/verdict_score) rounds to Fake/1.0 for most "
            "AI clips and is kept for audit only, not ranking."
        ),
        "ranked_by": "frame_mean",
        "winner": (
            {
                "filename": winner.name,
                "group": winner.group,
                "frame_mean": winner.frame_mean,
                "frame_min": winner.frame_min,
            }
            if winner
            else None
        ),
        "results": [
            {
                "rank": r.rank,
                "filename": r.name,
                "group": r.group,
                "frame_mean": r.frame_mean,
                "frame_min": r.frame_min,
                "frame_max": r.frame_max,
                "chunk_mean": r.chunk_mean,
                "frame_count": r.frame_count,
                "verdict_label": r.verdict_label,
                "verdict_score": r.verdict_score,
                "certainty": r.certainty,
                "status": r.status,
                "error": r.error,
            }
            for r in ordered
        ],
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    csv_path = root / REPORT_CSV_NAME

    def _c(v):
        return "" if v is None else v

    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
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
        )
        for r in ordered:
            writer.writerow(
                [
                    _c(r.rank),
                    r.name,
                    r.group,
                    _c(r.frame_mean),
                    _c(r.frame_min),
                    _c(r.frame_max),
                    _c(r.chunk_mean),
                    r.frame_count,
                    r.verdict_label,
                    _c(r.verdict_score),
                    _c(r.certainty),
                    r.status,
                ]
            )

    md_path = root / REPORT_MD_NAME
    md_path.write_text(
        _render_markdown(root, generated_at, ordered, winner),
        encoding="utf-8",
    )

    return json_path, csv_path, md_path
