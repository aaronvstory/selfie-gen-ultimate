"""Parse generated-video filenames into structured metadata.

The Selfie-Gen pipeline names files via append-only suffix chains:

    {image_stem}_{model_short}_p{slot}_{take}.mp4
    {image_stem}_{model_short}_p{slot}_{take}-oldcam-v{N}.mp4
    {image_stem}_{model_short}_p{slot}_{take}-oldcam-v{N}-rppg - {snr}-{phase}-{temporal}-{motion}-{harmonic}.mp4

We strip right-to-left (rppg -> oldcam -> kling) so each parser sees a
cleaner residual stem than the one before it. The Kling tail strip is
last, so the residual is the upstream image stem (which itself may
contain a ``_sim{N}_{idx}`` similarity marker from selfie generation).

The rPPG metric tail is split using the canonical
``automation.rppg.parse_metric_suffix`` so negative-phase handling stays
in one place. Regex anchors here purposely refuse to swallow rppg
metric tails or oldcam suffixes — strip order matters.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Reuse the canonical metric-suffix splitter so negative-phase handling
# stays consistent with the injector's output (automation/rppg.py:170).
from automation.rppg import parse_metric_suffix as _parse_metric_suffix


_KLING_TAIL_RE = re.compile(
    r"_(?P<model>[A-Za-z0-9]+)_p(?P<slot>\d+)_(?P<take>\d+)$"
)
_OLDCAM_TAIL_RE = re.compile(r"-oldcam-v(?P<v>\d+)$")
# Looped suffix from kling_gui/queue_manager.py::_loop_video — common
# downstream variant (`{kling_stem}_looped.mp4`) that the Kling tail
# regex can't see through because it requires _p{slot}_{take} to be at
# end-of-stem. We strip _looped BEFORE the Kling parse so looped+
# oldcam+rPPG chains still associate to their source image.
# Codex PR #43 P1 finding (3272768118).
_LOOPED_TAIL_RE = re.compile(r"_looped$")
_RPPG_BARE_TAIL = "-rppg"
_RPPG_METRIC_PREFIX = "-rppg - "
_SIMILARITY_RE = re.compile(r"_sim(?P<sim>\d+|na)_\d{3}")


@dataclass(frozen=True)
class RppgMetrics:
    """The 5 metrics the rPPG injector embeds in the filename tail.

    Order matches automation.rppg._METRIC_KEYS exactly.
    """

    snr: float
    phase: float
    temporal: float
    motion: float
    harmonic: float


@dataclass
class VideoMetadata:
    """Parsed video filename. All fields except path are optional —
    parsers gracefully degrade for files that don't match the pipeline
    naming convention (e.g. user-renamed mp4s)."""

    path: Path
    base_stem: str
    model_short: Optional[str] = None
    slot: Optional[int] = None
    take: Optional[int] = None
    is_looped: bool = False
    oldcam_version: Optional[int] = None
    rppg_metrics: Optional[RppgMetrics] = None
    rppg_metrics_source: Optional[str] = None  # "filename" | "sidecar" | None
    has_rppg: bool = False
    similarity: Optional[int] = None
    similarity_na: bool = False
    raw_suffixes: List[str] = field(default_factory=list)


def parse_rppg_segment(stem: str) -> tuple[str, bool, Optional[RppgMetrics]]:
    """Strip the rPPG suffix (with optional metric tail) from a stem.

    Returns (residual_stem, has_rppg, metrics_from_filename). Tries the
    metric-rename form first, then the bare ``-rppg`` form. Honours
    negative phase via the canonical splitter.
    """
    # Metric-rename form: "{prior}-rppg - <snr>-<phase>-<temporal>-<motion>-<harmonic>"
    # Find the LAST occurrence of the literal prefix — file stems can in
    # theory contain " - " elsewhere, though pipeline stems don't.
    idx = stem.rfind(_RPPG_METRIC_PREFIX)
    if idx != -1:
        prior = stem[:idx]  # e.g. "...-oldcam-v24"
        requested = f"{prior}-rppg"
        metrics_dict = _parse_metric_suffix(stem, requested)
        if metrics_dict is not None:
            return (
                prior,
                True,
                RppgMetrics(
                    snr=metrics_dict["snr"],
                    phase=metrics_dict["phase"],
                    temporal=metrics_dict["temporal"],
                    motion=metrics_dict["motion"],
                    harmonic=metrics_dict["harmonic"],
                ),
            )

    # Bare -rppg form (metrics-off, sidecar carries the numbers).
    if stem.endswith(_RPPG_BARE_TAIL):
        return (stem[: -len(_RPPG_BARE_TAIL)], True, None)

    return (stem, False, None)


def parse_oldcam_segment(stem: str) -> tuple[str, Optional[int]]:
    """Strip the ``-oldcam-vN`` tail; return (residual, version_int|None)."""
    m = _OLDCAM_TAIL_RE.search(stem)
    if m is None:
        return (stem, None)
    return (stem[: m.start()], int(m.group("v")))


def parse_looped_segment(stem: str) -> tuple[str, bool]:
    """Strip a trailing ``_looped`` token; return (residual, is_looped).

    Loop variants come from ``kling_gui/queue_manager.py::_loop_video``
    and are extremely common (one per generated Kling clip when the
    user has loop-output enabled). Without this strip the Kling-tail
    regex misses files like ``front_k25tStd_p4_1_looped.mp4`` because
    its anchor requires the ``_take`` digits at end-of-stem.

    Note: ``queue_manager`` actively refuses to double-loop a clip
    (``_looped_looped`` is impossible per the existing guard at
    queue_manager.py:723), so a single non-greedy strip is enough."""
    m = _LOOPED_TAIL_RE.search(stem)
    if m is None:
        return (stem, False)
    return (stem[: m.start()], True)


def parse_kling_segment(
    stem: str,
) -> tuple[str, Optional[str], Optional[int], Optional[int]]:
    """Strip the ``_{model}_p{slot}_{take}`` Kling tail.

    Returns (residual_image_stem, model_short, slot, take). Returns the
    input unchanged if no Kling tail is present (caller treats that as a
    non-pipeline file).
    """
    m = _KLING_TAIL_RE.search(stem)
    if m is None:
        return (stem, None, None, None)
    return (
        stem[: m.start()],
        m.group("model"),
        int(m.group("slot")),
        int(m.group("take")),
    )


def parse_similarity_from_stem(stem: str) -> tuple[Optional[int], bool]:
    """Extract the similarity marker from an upstream image stem.

    The selfie generator names files ``..._sim{N}_{idx}.png`` where N is
    a 0-100 score or the literal token ``na`` (no face / no match).
    Returns (score_or_None, similarity_na).
    """
    m = _SIMILARITY_RE.search(stem)
    if m is None:
        return (None, False)
    raw = m.group("sim")
    if raw == "na":
        return (None, True)
    try:
        return (int(raw), False)
    except ValueError:
        return (None, False)


def load_sidecar_metrics(video_path: Path) -> Optional[RppgMetrics]:
    """Load metrics from the ``<stem>.metrics.json`` sidecar if present.

    The injector writes this sidecar when metric-filename mode is off.
    Returns None for missing files, unreadable files, malformed JSON,
    or JSON missing any of the 5 required keys.

    Schema: ``automation.rppg.finalize_rppg_output`` writes the metrics
    NESTED under a "metrics" key:

        {
          "source": "<file>",
          "metrics": {"snr": ..., "phase": ..., "temporal": ...,
                      "motion": ..., "harmonic": ...},
          "order": ["snr", "phase", ...]
        }

    We read from the nested ``metrics`` first (canonical producer
    format) AND fall back to top-level for safety in case a manual /
    third-party sidecar uses the flat schema.
    Codex P2 (3272968651) on PR #43: my first implementation only read
    top-level which silently dropped every real sidecar.
    """
    sidecar = video_path.with_suffix("").with_suffix(".metrics.json")
    # ``with_suffix("").with_suffix("...")`` strips one suffix; for
    # ``a.mp4`` -> ``a`` -> ``a.metrics.json``. For ``a-rppg.mp4`` -> ``a-rppg``
    # -> ``a-rppg.metrics.json`` which matches what the injector writes.
    if not sidecar.is_file():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Prefer the nested schema (canonical producer format), fall back
    # to flat top-level for legacy / third-party sidecars.
    payload = data.get("metrics")
    if not isinstance(payload, dict):
        payload = data
    try:
        return RppgMetrics(
            snr=float(payload["snr"]),
            phase=float(payload["phase"]),
            temporal=float(payload["temporal"]),
            motion=float(payload["motion"]),
            harmonic=float(payload["harmonic"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_video_filename(path: Path) -> VideoMetadata:
    """Parse a video Path into structured metadata.

    Strip order is right-to-left: rPPG tail -> oldcam tail -> Kling
    tail. The remaining stem is the upstream image stem, which is
    scanned for the ``_sim{N}_{idx}`` similarity marker.

    Files that don't match the pipeline convention return a
    VideoMetadata with only ``path`` and ``base_stem`` set (the stem
    is the input filename's stem unchanged).
    """
    p = Path(path)
    stem = p.stem
    raw_suffixes: List[str] = []

    stem, has_rppg, filename_metrics = parse_rppg_segment(stem)
    if has_rppg:
        raw_suffixes.append("rppg")

    stem, oldcam_v = parse_oldcam_segment(stem)
    if oldcam_v is not None:
        raw_suffixes.append(f"oldcam-v{oldcam_v}")

    # _looped strip sits BETWEEN oldcam and kling because the loop step
    # runs AFTER kling (kling_gui/queue_manager.py:1101) but BEFORE
    # oldcam (per the rPPG fan-out priority list at line 821-830).
    # Real filename chains we see in the wild:
    #     {stem}_k25tStd_p4_1.mp4                       (raw kling)
    #     {stem}_k25tStd_p4_1_looped.mp4                (looped kling)
    #     {stem}_k25tStd_p4_1_looped-oldcam-v24.mp4     (looped + oldcam)
    #     {stem}_k25tStd_p4_1_looped-oldcam-v24-rppg - ...mp4  (+ rppg)
    # So strip order in REVERSE is: rppg -> oldcam -> _looped -> kling.
    stem, is_looped = parse_looped_segment(stem)
    if is_looped:
        raw_suffixes.append("looped")

    image_stem, model_short, slot, take = parse_kling_segment(stem)
    similarity, similarity_na = parse_similarity_from_stem(image_stem)

    # Filename order is rppg-outermost, but pipeline-stage order is
    # rppg-after-oldcam-after-kling. Reverse so the suffix list reads
    # in pipeline order (oldcam, then rppg).
    raw_suffixes.reverse()

    metrics = filename_metrics
    metrics_source: Optional[str] = "filename" if filename_metrics else None
    if has_rppg and metrics is None:
        sidecar = load_sidecar_metrics(p)
        if sidecar is not None:
            metrics = sidecar
            metrics_source = "sidecar"

    return VideoMetadata(
        path=p,
        base_stem=image_stem,
        model_short=model_short,
        slot=slot,
        take=take,
        is_looped=is_looped,
        oldcam_version=oldcam_v,
        rppg_metrics=metrics,
        rppg_metrics_source=metrics_source,
        has_rppg=has_rppg,
        similarity=similarity,
        similarity_na=similarity_na,
        raw_suffixes=raw_suffixes,
    )
