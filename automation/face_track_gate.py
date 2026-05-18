"""Face-track-continuity gate for the automation pipeline.

Empirical basis: `docs/analysis/versailles_fail_vs_pass.md`. On the
labelled Persona corpus, clips whose face becomes untrackable (esp. in
the ~5-8s head-turn window) fail the Persona liveness check far more
often than clips that hold a face throughout. Face-track continuity of
the **Kling source** is the only signal that discriminated FAIL vs PASS
across the corpus; low track% is a strong (graded) FAIL predictor.

This gate runs right after `video_generate` and before `oldcam`: it
samples the freshly generated video and, if the face-track percentage is
below a configurable threshold, flags the case for manual review /
regeneration *before* spending the oldcam pass + a Persona attempt on a
clip that is unlikely to pass.

Pure OpenCV + MediaPipe FaceLandmarker (the same dependency oldcam v9-11
already use). Degrades safely: if cv2/mediapipe or the landmarker model
is unavailable, the gate returns ``available=False`` and the pipeline
treats it as a non-blocking skip (never hard-fails a case on tooling).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Default sampling: 8 fps is enough to catch multi-frame dropouts while
# staying fast; 96.0% is the empirical boundary (the lowest-tracking PASS
# in the expanded omnapayments corpus sat at ~96%, every clear FAIL well
# below). Tunable via automation_facetrack_* config keys.
DEFAULT_SAMPLE_FPS = 8.0
DEFAULT_MIN_TRACK_PCT = 96.0


@dataclass
class FaceTrackResult:
    available: bool
    track_pct: Optional[float] = None
    sampled: int = 0
    with_face: int = 0
    longest_gap_s: Optional[float] = None
    passed: bool = True          # True unless we have a real sub-threshold result
    reason: str = ""

    def to_meta(self) -> dict:
        return {
            "facetrack_available": self.available,
            "facetrack_pct": self.track_pct,
            "facetrack_sampled": self.sampled,
            "facetrack_longest_gap_s": self.longest_gap_s,
            "facetrack_passed": self.passed,
            "facetrack_reason": self.reason,
        }


def _resolve_model(repo_root: Path, explicit: Optional[str]) -> Optional[str]:
    if explicit and os.path.exists(explicit):
        return explicit
    env = os.environ.get("MEDIAPIPE_FACE_LANDMARKER_MODEL")
    if env and os.path.exists(env):
        return env
    cand = repo_root / "face_landmarker.task"
    return str(cand) if cand.exists() else None


def measure_face_track(
    video_path: str,
    repo_root: Path,
    *,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    min_track_pct: float = DEFAULT_MIN_TRACK_PCT,
    model_path: Optional[str] = None,
) -> FaceTrackResult:
    """Sample *video_path* and report face-track continuity.

    Never raises: any tooling problem yields available=False, passed=True
    (non-blocking) so a missing model / cv2 cannot fail a pipeline case.
    """
    try:
        import cv2  # noqa: WPS433 (lazy: optional heavy dep)
        import mediapipe as mp  # noqa: WPS433
    except Exception as exc:  # pragma: no cover - import guard
        return FaceTrackResult(False, reason=f"cv2/mediapipe unavailable: {exc}")

    model = _resolve_model(repo_root, model_path)
    if not model:
        return FaceTrackResult(False, reason="face_landmarker.task not found")

    if not os.path.exists(video_path):
        return FaceTrackResult(False, reason=f"video missing: {video_path}")

    cap = None
    landmarker = None
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return FaceTrackResult(False, reason="cannot open video")
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        step = max(1, int(round(src_fps / sample_fps)))

        base = mp.tasks.BaseOptions(model_asset_path=model)
        opts = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=base,
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            num_faces=1,
        )
        landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(opts)

        sampled = with_face = 0
        cur_gap = longest_gap = 0
        idx = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % step == 0:
                sampled += 1
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                if landmarker.detect(mp_img).face_landmarks:
                    with_face += 1
                    cur_gap = 0
                else:
                    cur_gap += 1
                    longest_gap = max(longest_gap, cur_gap)
            idx += 1
    except Exception as exc:  # pragma: no cover - runtime guard
        return FaceTrackResult(False, reason=f"face-track error: {exc}")
    finally:
        # Guaranteed cleanup: an exception mid-loop (e.g. in
        # landmarker.detect) must not leak a VideoCapture / landmarker
        # handle — these accumulate fast across a batch pipeline run.
        if cap is not None:
            try:
                cap.release()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    if sampled == 0:
        return FaceTrackResult(False, reason="no frames sampled")

    pct = round(100.0 * with_face / sampled, 2)
    passed = pct >= min_track_pct
    return FaceTrackResult(
        available=True,
        track_pct=pct,
        sampled=sampled,
        with_face=with_face,
        longest_gap_s=round(longest_gap / sample_fps, 2),
        passed=passed,
        reason=(
            ""
            if passed
            else f"face-track {pct}% < {min_track_pct}% threshold "
            f"(likely fails Persona — regenerate the source)"
        ),
    )
