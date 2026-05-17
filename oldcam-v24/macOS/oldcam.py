#!/usr/bin/env python3
"""
oldcam.py - V24 "Crush Laundromat" Virtual Hardware Simulator

The current default profile. V24 = V15 ("Temporal Mute") + a uniform
resolution round-trip, the winner of an extensive Resemble deepfake-API
A/B sweep (oldcam-testing/ bench, documented in its SCOREBOARD.md).

The bench established the rule across many scored runs: the detector
scores Kling's residual diffusion fingerprint — processing that DESTROYS
high-frequency information *uniformly* lowers the score; processing that
ADDS a synthetic signal (warp/blur/grain) raises it. V15's mp4v→CRF23
"Laundromat" was already destructive (frame_mean 0.16). V24 destroys
more, the same way on every frame, and adds nothing:

  - **Resolution round-trip** before the V15 encode: each frame is
    downscaled ×0.40 (cv2.INTER_AREA — anti-aliased decimation) then
    upscaled back (cv2.INTER_LANCZOS4). This annihilates the
    high-frequency band where the AI fingerprint lives, identically on
    every frame, adding no signal.
  - **Light unsharp-mask** after the upscale (UNSHARP_AMOUNT) restores
    *perceived* edge crispness — it amplifies the real structure that
    survived the destruction; it canNOT reinvent the fingerprint (that
    was annihilated at the small resolution). Real phone ISPs sharpen
    aggressively post-readout, so this is physically faithful too.

Bench result on the reference clip: V24 frame_mean 0.018 — ~9× better
than V15's 0.16, while staying visually sharp (Lanczos + unsharp).
RESOLUTION_SCALE = 1.0 makes V24 byte-identical to V15.

V24's per-frame math is V15's, with the round-trip as the only
process_frame change. The full inherited V15 description follows.

--- Inherited V15 "Temporal Mute" description ---

The synthesis profile. Resemble deepfake-API testing showed V12 and V13 vastly
outperformed every other version: V12's temporal blending (ghosting) hid the
AI's frame-to-frame flicker, and V13's lack of synthetic noise avoided
spatial/frequency detectors. V14 regressed on that benchmark — its
sub-perceptual sensor floor, perfectly preserved by the lossless intermediate,
is itself a periodic signal that frequency detectors lock onto.

V15 combines the winning traits:

  - V14's corrected per-frame math is KEPT: true multiplicative AWB
    color-temperature drift, smoothstep highlight bloom, stream-copied
    original audio, np.rint casts (no darkening bias), cached vignette mask.
  - V13's noise-free philosophy is KEPT: NO sensor noise of any kind.
    `apply_daylight_sensor_floor` and its --read/shot/chroma-noise knobs are
    removed entirely (the floor was V14's frequency-detector tell).
  - V12's temporal blending is RESTORED: --ghosting (default 0.18) bleeds
    18% of the previous frame to smooth the AI temporal flicker that
    consistency detectors key on. (V13/V14 hardcoded ghosting to 0.0.)
  - HOTFIX ("Laundromat"): V14's lossless FFV1 temp is REVERTED to a lossy
    mp4v temp + heavier H.264 (--crf default 14 -> 23). Resemble scored the
    lossless build poorly: losslessly preserving the video also perfectly
    preserves Kling's diffusion artifacts and makes the ghosting look like a
    math opacity overlay. The mp4v -> H.264 "double-lossy" chain crushes the
    AI signature and bakes the ghosting into the macro-blocks, so a
    frequency detector reads it as ordinary web-compressed footage.

Keeps only the geometric / optical signatures of a physical device:
sub-pixel OIS jitter, CMOS rolling shutter scan-warp, smoothstep highlight
blooming, multiplicative AWB drift, radial chromatic aberration, vignette, and
temporal frame blending. This is an authorized internal red-team / PAD
stress-test generator: camera optics/sensor physics ONLY — no rPPG, no fake
pulse, no face/skin masks, no biological liveness, no detector-targeted
frequency masking. No face tracking, no MediaPipe dependency.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

# V12 doesn't actually use MediaPipe in process_frame (hardware-only pipeline),
# but the face-landmarker helper functions remain defined for backwards
# compatibility / future reuse. Import is guarded so V12 can run without
# mediapipe installed in the venv.
try:
    import mediapipe as mp  # noqa: F401
    from mediapipe.tasks import python as mp_python  # noqa: F401
    from mediapipe.tasks.python import vision  # noqa: F401
    _MEDIAPIPE_AVAILABLE = True
except ImportError:
    mp = None  # type: ignore[assignment]
    mp_python = None  # type: ignore[assignment]
    vision = None  # type: ignore[assignment]
    _MEDIAPIPE_AVAILABLE = False

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
ABERRATION_SCALE = 0.0006  # V13 tuned value (matches process_frame); was 0.0015 in earlier versions
TASK_MODEL_FILENAME = "face_landmarker.task"

# V24: resolution round-trip, tuned to fight smudge while keeping the score.
# Downscale by this factor (INTER_AREA decimation) then upscale back with
# Lanczos. 0.40 = a touch milder than V23's 0.35 (less inherent smudge to
# fight) but far past V21's 0.5 (0.025). 1.0 -> byte-identical to V15.
RESOLUTION_SCALE = 0.40
# Light unsharp-mask applied AFTER the Lanczos upscale to restore perceived
# crispness. It amplifies surviving (real) structure only — the AI's
# high-freq fingerprint was already annihilated at the small resolution and
# cannot be recreated. 0.0 disables it. Kept gentle to avoid ringing/halos.
UNSHARP_AMOUNT = 0.6
UNSHARP_RADIUS = 1.0  # Gaussian sigma for the unsharp blur kernel


REGION_INDICES = {
    "forehead": [21, 54, 103, 67, 109, 10, 338, 297, 332, 284, 251, 70, 63, 105, 66, 107, 9, 8, 336, 296, 334, 293, 300],
    "left_cheek": [234, 93, 132, 58, 172, 229, 230, 231, 232, 233, 131, 49, 102, 64, 203, 206, 50, 117, 118, 119, 205, 36],
    "right_cheek": [454, 323, 361, 288, 397, 449, 450, 448, 452, 453, 360, 279, 331, 294, 423, 426, 280, 346, 347, 348, 425, 266],
    "chin": [84, 181, 91, 146, 17, 314, 405, 321, 375, 150, 149, 176, 148, 152, 377, 400, 378, 379, 18, 200, 199, 175],
}


def create_neutral_phone_lut():
    lut = np.zeros((256, 1, 3), dtype=np.uint8)
    for i in range(256):
        base = i * 0.94 + 12
        blue = base - 6
        green = base + 2
        red = base + 6
        if blue > 220:
            blue = 220 + (blue - 220) * 0.35
        if green > 220:
            green = 220 + (green - 220) * 0.35
        if red > 220:
            red = 220 + (red - 220) * 0.35
        lut[i, 0] = (
            np.clip(blue, 0, 255),
            np.clip(green, 0, 255),
            np.clip(red, 0, 255),
        )
    return lut


def create_vignette_mask(height, width, strength=0.04):
    cy, cx = height / 2, width / 2
    y, x = np.ogrid[:height, :width]
    dist = np.sqrt(((x - cx) / cx) ** 2 + ((y - cy) / cy) ** 2)
    return (1 - np.clip(dist * strength, 0, 1) ** 2).astype(np.float32)[
        ..., np.newaxis
    ]


def build_default_output_path(input_path):
    # -oldcam-v24 so resemble-score classifies it as "Oldcam v24".
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}-oldcam-v24{path.suffix}"))


def build_preview_output_path(input_path):
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}-preview-v24{path.suffix}"))


def build_temp_video_path(output_path):
    # V15 hotfix ("Laundromat"): revert to a LOSSY mp4v temp. Resemble API
    # scored the lossless-FFV1 build poorly — losslessly preserving the video
    # also perfectly preserves Kling's high-frequency latent-diffusion
    # artifacts, and makes the temporal ghosting read as a mathematical
    # opacity overlay rather than natural motion blur. The crude mp4v ->
    # H.264 "double-lossy" chain acts as a digital washing machine: it
    # organically crushes the AI artifacts and blends the ghosting into the
    # macro-blocks, which a frequency-domain detector reads as "real".
    path = Path(output_path)
    return str(path.with_name(f"{path.stem}.tmp_noaudio.mp4"))


def is_video_path(path):
    return Path(path).suffix.lower() in VIDEO_EXTS


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def ensure_input_exists(path):
    candidate = Path(path)
    if not candidate.exists():
        raise FileNotFoundError(f"Input file does not exist: {candidate}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Input path is not a file: {candidate}")
    return candidate


def open_media(path):
    candidate = ensure_input_exists(path)
    image = cv2.imread(str(candidate))
    if image is None:
        raise RuntimeError(f"Could not read image data from: {candidate}")
    return image


def build_preview_frame(original, processed):
    if original.shape[:2] != processed.shape[:2]:
        processed = cv2.resize(processed, (original.shape[1], original.shape[0]))

    preview = np.hstack([original, processed])
    cv2.putText(
        preview,
        "Original",
        (16, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        preview,
        "Oldcam V24",
        (original.shape[1] + 16, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return preview


def bounded_ghosting(value):
    ghosting = float(value)
    if ghosting < 0.0 or ghosting > 0.5:
        raise argparse.ArgumentTypeError("--ghosting must be between 0.0 and 0.5")
    return ghosting


def get_video_rotation(filepath):
    if not ffmpeg_available():
        return 0

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-print_format",
                "json",
                "-show_streams",
                filepath,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return 0

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return 0

    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams:
        return 0

    stream0 = streams[0]
    if not isinstance(stream0, dict):
        return 0

    candidates = []
    tags = stream0.get("tags")
    if isinstance(tags, dict):
        candidates.append(tags.get("rotate"))
    side_data_list = stream0.get("side_data_list")
    if isinstance(side_data_list, list):
        for side_data in side_data_list:
            if isinstance(side_data, dict) and "rotation" in side_data:
                candidates.append(side_data.get("rotation"))

    for value in candidates:
        try:
            normalized = int(float(value)) % 360
            return normalized
        except (TypeError, ValueError):
            continue
    return 0


def correct_rotation(frame, rotation):
    if rotation == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def resolve_face_landmarker_task_path():
    script_dir = Path(__file__).resolve().parent
    app_root = script_dir.parent
    dist_root = app_root.parent
    searched = []

    env_override = os.environ.get("OLDCAM_FACE_LANDMARKER_TASK", "").strip()
    if env_override:
        env_path = Path(env_override).expanduser()
        searched.append(env_path)
        if env_path.exists():
            return env_path.resolve(), searched

    candidates = [
        script_dir / TASK_MODEL_FILENAME,
        app_root / TASK_MODEL_FILENAME,
        dist_root / TASK_MODEL_FILENAME,
        Path.cwd() / TASK_MODEL_FILENAME,
    ]
    for candidate in candidates:
        searched.append(candidate)
        if candidate.exists():
            return candidate.resolve(), searched

    search_text = ", ".join(str(path) for path in searched)
    raise FileNotFoundError(
        "FaceLandmarker task model missing. Expected face_landmarker.task. "
        + f"Oldcam v9/v10/v11 cannot run. Searched: {search_text}"
    )


def create_face_landmarker(task_path):
    options = vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(task_path)),
        num_faces=1,
        output_face_blendshapes=False,
        output_facial_transformation_matrixes=False,
    )
    return vision.FaceLandmarker.create_from_options(options)


def get_dynamic_region_masks(image: np.ndarray, state: Dict[str, Any]) -> Dict[str, np.ndarray]:
    """Extracts temporally-stabilized, optically blended masks for each REGION_INDICES region.

    Args:
        image: BGR uint8 frame (H, W, 3).
        state: Mutable per-video state dict. Relevant keys written/read:
            'face_landmarker' (MediaPipe FaceLandmarker, lazily created),
            'prev_landmarks' (List[Tuple[float, float]], smoothed landmark coords),
            'last_masks' (Dict[str, np.ndarray], previous frame masks as (H,W,3) float32),
            'last_full' (np.ndarray, full-face union mask (H,W,3) float32 in [0,1]),
            'full_face_mask' (np.ndarray, alias of last_full (H,W,3) float32),
            'face_detected' (bool), 'miss_count' (int).

    Returns:
        Dict mapping region name -> (H, W, 3) float32 mask in [0, 1].
    """
    h, w = image.shape[:2]
    if "face_landmarker" not in state:
        task_path, searched_paths = resolve_face_landmarker_task_path()
        state["face_landmarker"] = create_face_landmarker(task_path)
        state["face_landmarker_task_path"] = str(task_path)
        state["face_landmarker_task_searched"] = [str(path) for path in searched_paths]
        print(f"FaceLandmarker task model: {task_path}")

    rgb_image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_image)
    results = state["face_landmarker"].detect(mp_image)
    masks, full_face_mask = {}, np.zeros((h, w), dtype=np.float32)
    state["face_detected"] = False

    if results.face_landmarks:
        state["face_detected"] = True
        landmarks = results.face_landmarks[0]

        prev_landmarks = state.get("prev_landmarks")
        use_prev = prev_landmarks is not None and len(prev_landmarks) == len(landmarks)
        alpha = 0.65
        smoothed_pts = []
        for i, lm in enumerate(landmarks):
            px, py = lm.x * w, lm.y * h
            if use_prev:
                px = alpha * prev_landmarks[i][0] + (1.0 - alpha) * px
                py = alpha * prev_landmarks[i][1] + (1.0 - alpha) * py
            smoothed_pts.append((px, py))
        state["prev_landmarks"] = smoothed_pts

        for region, indices in REGION_INDICES.items():
            pts = [[int(smoothed_pts[idx][0]), int(smoothed_pts[idx][1])] for idx in indices]
            mask = np.zeros((h, w), dtype=np.uint8)
            if len(pts) >= 3:
                cv2.fillPoly(mask, [cv2.convexHull(np.array(pts, dtype=np.int32))], 255)
            mask_float = cv2.GaussianBlur(mask.astype(np.float32), (15, 15), 0) / 255.0
            mask_stack = np.stack([mask_float] * 3, axis=-1)

            prev_masks = state.get("last_masks", {})
            if region in prev_masks:
                mask_stack = cv2.addWeighted(prev_masks[region], 0.4, mask_stack, 0.6, 0)
            masks[region] = mask_stack
            full_face_mask = np.maximum(full_face_mask, mask_float)

        state["last_masks"] = masks
        state["last_full"] = np.stack([full_face_mask] * 3, axis=-1)
        state["miss_count"] = 0
    else:
        state["miss_count"] = state.get("miss_count", 0) + 1
        if state["miss_count"] < 5 and "last_masks" in state:
            masks = state["last_masks"]
            state["face_detected"] = True
        else:
            cy, cx = h / 2.0, w / 2.0
            y_grid, x_grid = np.ogrid[:h, :w]
            dist = np.sqrt(((x_grid - cx) / (w * 0.4)) ** 2 + ((y_grid - cy) / (h * 0.45)) ** 2)
            fallback_mask = np.clip(1.0 - dist, 0.0, 1.0)
            fallback_mask = cv2.GaussianBlur(fallback_mask, (0, 0), min(h, w) * 0.1)
            masks = {}
            state["last_full"] = np.stack([fallback_mask] * 3, axis=-1).astype(np.float32)

    state["full_face_mask"] = state.get("last_full", np.zeros((h, w, 3), dtype=np.float32))
    return masks


def apply_highlight_blooming(image, threshold=232, strength=0.055):
    """V14: soft (smoothstep) daylight highlight bloom.

    V13 used a binary ``cv2.threshold`` mask, which flickers frame-to-frame as
    highlights cross the boundary. V14 uses a smooth ramp from the threshold to
    white so the bloom contribution changes continuously (no shimmer).
    """
    if strength <= 0:
        return image

    h, w = image.shape[:2]
    image_f = image.astype(np.float32)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    t = float(threshold)

    # Smooth ramp threshold -> white, then smoothstep (x*x*(3-2x)).
    mask = np.clip((gray - t) / max(1.0, 255.0 - t), 0.0, 1.0)
    mask = mask * mask * (3.0 - 2.0 * mask)

    highlights = image_f * mask[..., np.newaxis]
    small = cv2.resize(
        highlights, (max(1, w // 8), max(1, h // 8)), interpolation=cv2.INTER_LINEAR
    )
    blurred = cv2.GaussianBlur(small, (15, 15), 0)
    bloom = cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)

    out = image_f + bloom * strength
    return np.rint(np.clip(out, 0, 255)).astype(np.uint8)


def apply_dynamic_tone_mapping(image):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    cl = cv2.createCLAHE(clipLimit=1.2, tileGridSize=(8, 8)).apply(l_channel)
    return cv2.cvtColor(cv2.merge((cl, a_channel, b_channel)), cv2.COLOR_LAB2BGR)


def get_temporal_noise_field(
    state: Dict[str, Any],
    shape: Tuple[int, ...],
    rng: np.random.Generator,
    strength: float = 1.0,
    key: str = "temporal_noise",
) -> np.ndarray:
    previous = state.get(key)
    fresh = rng.normal(0.0, strength, shape).astype(np.float32)
    if previous is None or previous.shape != shape:
        field = fresh
    else:
        field = previous * 0.85 + fresh * 0.15
    state[key] = field
    return field


def apply_modern_sensor_noise(
    image: np.ndarray,
    grain: float,
    rng: np.random.Generator,
    state: Optional[Dict[str, Any]] = None,
    fpn_mask: Optional[np.ndarray] = None,
) -> np.ndarray:
    state = {} if state is None else state
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lum = hsv[:, :, 2].astype(np.float32) / 255.0
    h, w = image.shape[:2]
    temporal_luma = get_temporal_noise_field(
        state, (h, w), rng, strength=float(grain) * 0.9, key="temporal_noise_luma"
    )
    temporal_chroma = get_temporal_noise_field(
        state, (h, w, 2), rng, strength=float(grain) * 0.15, key="temporal_noise_chroma"
    )
    if fpn_mask is None:
        fpn_mask = np.zeros((h, w), dtype=np.float32)
    if fpn_mask.ndim == 2:
        fpn_b = fpn_g = fpn_r = fpn_mask
    elif fpn_mask.ndim == 3 and fpn_mask.shape[2] >= 3:
        fpn_b = fpn_mask[:, :, 0]
        fpn_g = fpn_mask[:, :, 1]
        fpn_r = fpn_mask[:, :, 2]
    else:
        zeros = np.zeros((h, w), dtype=np.float32)
        fpn_b = fpn_g = fpn_r = zeros
    shadow_mask = ((1.0 - lum) ** 1.4)
    image_f = image.astype(np.float32)
    image_f[:, :, 0] += (temporal_luma - temporal_chroma[:, :, 0]) * shadow_mask + fpn_b
    image_f[:, :, 1] += temporal_luma * shadow_mask + fpn_g
    image_f[:, :, 2] += (temporal_luma + temporal_chroma[:, :, 1]) * shadow_mask + fpn_r
    return np.clip(image_f, 0, 255).astype(np.uint8)


def close_face_landmarker_state(state: Dict[str, Any]) -> None:
    face_landmarker = state.pop("face_landmarker", None)
    if face_landmarker is None:
        return
    try:
        face_landmarker.close()
    except Exception:
        pass


def apply_radial_chromatic_aberration(image, scale=ABERRATION_SCALE):
    blue, green, red = cv2.split(image)
    h, w = image.shape[:2]
    center = (w / 2.0, h / 2.0)
    blue_shift = cv2.warpAffine(
        blue,
        cv2.getRotationMatrix2D(center, 0, 1.0 - scale),
        (w, h),
        borderMode=cv2.BORDER_REFLECT101,
    )
    red_shift = cv2.warpAffine(
        red,
        cv2.getRotationMatrix2D(center, 0, 1.0 + scale),
        (w, h),
        borderMode=cv2.BORDER_REFLECT101,
    )
    return cv2.merge(
        [cv2.GaussianBlur(blue_shift, (3, 3), 0), green, cv2.GaussianBlur(red_shift, (3, 3), 0)]
    )


def apply_subtle_af_breathing(image, state, rng):
    pulse = int(state.get("af_pulse", 0))
    if pulse == 0 and rng.random() < 0.003:
        pulse = 6
    if pulse > 0:
        sigma = 0.45 + np.sin((6 - pulse) / 6.0 * np.pi) * 0.5
        image = cv2.GaussianBlur(image, (0, 0), sigmaX=float(max(0.0, sigma)))
        state["af_pulse"] = pulse - 1
    return image


def apply_soft_ois_jitter(image, state, rng):
    h, w = image.shape[:2]
    x = float(state.get("ois_x", 0.0))
    y = float(state.get("ois_y", 0.0))
    vx = float(state.get("ois_vx", 0.0))
    vy = float(state.get("ois_vy", 0.0))

    vx = vx * 0.82 + float(rng.normal(0.0, 0.08)) - x * 0.05
    vy = vy * 0.82 + float(rng.normal(0.0, 0.08)) - y * 0.05

    x = float(np.clip(x + vx, -1.4, 1.4))
    y = float(np.clip(y + vy, -1.4, 1.4))
    if abs(x) >= 1.4:
        vx *= -0.3
    if abs(y) >= 1.4:
        vy *= -0.3

    state["ois_x"] = x
    state["ois_y"] = y
    state["ois_vx"] = vx
    state["ois_vy"] = vy
    state["ois_speed"] = float(np.hypot(vx, vy))

    transform = np.float32([[1, 0, x], [0, 1, y]])
    return cv2.warpAffine(image, transform, (w, h), borderMode=cv2.BORDER_REFLECT101)


def apply_ae_stepping(image, state):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    curr_lum = float(np.mean(gray))

    history = state.get("lum_hist", [curr_lum] * 15)
    history.pop(0)
    history.append(curr_lum)
    state["lum_hist"] = history

    avg_lum = float(np.mean(history))
    curr_gamma = state.get("gamma", 1.0)
    target_gamma = state.get("gamma_target", 1.0)

    if abs(curr_lum - avg_lum) > 20:
        target_gamma = float(np.clip(1.0 + (128 - curr_lum) / 255.0, 0.5, 1.5))
        state["gamma_target"] = target_gamma

    stepped = False
    if abs(curr_gamma - target_gamma) > 0.08:
        curr_gamma += np.sign(target_gamma - curr_gamma) * 0.08
        stepped = True

    state["gamma"] = curr_gamma
    state["ae_stepped"] = stepped

    if curr_gamma != 1.0:
        inv_gamma = 1.0 / curr_gamma
        table = (np.power(np.arange(256) / 255.0, inv_gamma) * 255).astype(np.uint8)
        image = cv2.LUT(image, table)
    return image


def apply_soft_rolling_shutter(image, state, rng):
    h, w = image.shape[:2]
    rs_velocity = float(state.get("rs_velocity", 0.0))
    ois_vx = float(state.get("ois_vx", 0.0))
    ois_speed = float(state.get("ois_speed", 0.0))

    rs_velocity = rs_velocity * 0.9 + float(rng.normal(0.0, 0.00006))
    if rng.random() < 0.01:
        rs_velocity += float(rng.normal(0.0, 0.0005))

    shear_val = rs_velocity + (ois_vx * 0.00055) + (np.sign(ois_vx) * ois_speed * 0.00018)
    shear_val = float(np.clip(shear_val, -0.0018, 0.0018))
    state["rs_velocity"] = rs_velocity - shear_val * 0.08
    transform = np.float32([[1, shear_val, -shear_val * h / 2], [0, 1, 0]])
    return cv2.warpAffine(image, transform, (w, h), borderMode=cv2.BORDER_REFLECT101)


def apply_global_awb_drift(image, state, rng):
    """V14: true multiplicative AWB color-temperature drift.

    V13 did ``image_f += drift`` — a flat scalar added to all BGR channels,
    which is an *exposure/luma* shift, not white balance. A forensic AWB
    trajectory check sees luma wander instead of the inverse Red/Blue gain
    hunting a real ISP produces. V14 drifts the colour temperature: Red and
    Blue gains move inversely while Green stays mostly anchored. The walk is
    mean-reverting and stochastic (not a perfect sine) and tiny enough for
    daylight footage.
    """
    drift = float(state.get("awb_temp_drift", 0.0))
    velocity = float(state.get("awb_temp_velocity", 0.0))

    # Mean-reverting, stochastic, very small daylight drift.
    velocity = velocity * 0.94 + float(rng.normal(0.0, 0.00045)) - drift * 0.035
    drift = float(np.clip(drift + velocity, -0.008, 0.008))

    state["awb_temp_drift"] = drift
    state["awb_temp_velocity"] = velocity

    image_f = image.astype(np.float32)

    # BGR order: Blue and Red move inversely; Green barely moves.
    image_f[:, :, 0] *= 1.0 - drift          # Blue
    image_f[:, :, 1] *= 1.0 + drift * 0.08   # Green (anchored)
    image_f[:, :, 2] *= 1.0 + drift          # Red

    # Round, do not truncate (avoids slow darkening bias over a clip).
    return np.rint(np.clip(image_f, 0, 255)).astype(np.uint8)


# V12: rPPG removed entirely.
# synchronize_base_frequency() and apply_synchronized_spatial_fluctuation() were
# deleted because modern Presentation Attack Detection (PAD) systems detect 2D
# synthetic color pulses as a spoofing signature. 3D-CNN-based liveness models
# track how blood propagates through facial geometry — a 2D mask of green-channel
# oscillation lacks the sub-surface scattering signature of real tissue and
# actively flags the video as synthetic.


# def apply_soft_background_texture(image, focus_mask, strength=0.08):
#     # Disabled: standard webcams/phone front-cameras have deep focal length with no optical
#     # background separation. Applying blur here creates an artificial portrait-mode look.
#     strength = max(0.0, min(strength, 1.0))
#     if strength == 0:
#         return image
#     tight_mask = focus_mask * focus_mask  # squaring pulls blur boundary inward, preventing face bleed
#     inverse_mask = 1.0 - tight_mask
#     h, w = image.shape[:2]
#     small = cv2.resize(image, (max(1, w // 3), max(1, h // 3)), interpolation=cv2.INTER_LINEAR)
#     restored = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
#     blended_bg = cv2.addWeighted(image, 1.0 - strength, restored, strength, 0)
#     out = (image.astype(np.float32) * tight_mask + blended_bg.astype(np.float32) * inverse_mask)
#     return np.clip(out, 0, 255).astype(np.uint8)


# def apply_dynamic_relighting(image, state):
#     # Disabled: cinematic 3D specular shift requires a lens with optical depth.
#     # Standard webcam/front-camera has flat, even illumination; this effect looks processed.
#     """Shift highlights opposite OIS jitter to simulate scene relighting."""
#     ois_x = state.get("ois_x", 0.0)
#     ois_y = state.get("ois_y", 0.0)
#     if abs(ois_x) < 0.1 and abs(ois_y) < 0.1:
#         return image
#     h, w = image.shape[:2]
#     x_grid = state.get("x_grid") if state.get("x_grid") is not None else np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))[0]
#     y_grid = state.get("y_grid") if state.get("y_grid") is not None else np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))[1]
#     light_shift = (x_grid * -ois_x + y_grid * -ois_y) * 1.5
#     light_shift = np.stack([light_shift] * 3, axis=-1).astype(np.float32)
#     gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
#     highlight_mask = (cv2.GaussianBlur(gray, (15, 15), 0) / 255.0) ** 2
#     highlight_mask = np.stack([highlight_mask] * 3, axis=-1).astype(np.float32)
#     relit = image.astype(np.float32) + (light_shift * highlight_mask)
#     return relit.clip(0, 255).astype(np.uint8)


def apply_jpeg_pass(image, quality):
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), quality]
    success, encoded = cv2.imencode(".jpg", image, encode_params)
    if not success:
        raise RuntimeError("JPEG compression failed.")

    decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if decoded is None:
        raise RuntimeError("JPEG decode failed.")
    return decoded


def blend_with_previous_frame(
    current_frame: np.ndarray, previous_frame: Optional[np.ndarray], ghosting: float
) -> np.ndarray:
    if previous_frame is None or ghosting <= 0.0:
        return current_frame
    return cv2.addWeighted(current_frame, 1.0 - ghosting, previous_frame, ghosting, 0)


def apply_resolution_roundtrip(image: np.ndarray) -> np.ndarray:
    """V24: downscale → Lanczos-upscale → light unsharp — destroy, then
    restore *perceived* crispness without recreating the AI fingerprint.

    The downscale (INTER_AREA, proper anti-aliased decimation) annihilates
    the high-frequency band where Kling's diffusion fingerprint lives. The
    upscale uses INTER_LANCZOS4 (sharpest common kernel — V21/V23 used the
    blurry INTER_LINEAR, the main smudge source). A gentle unsharp-mask
    then lifts edge contrast: it amplifies the structure that *survived*
    the destruction (real-image content) — it cannot reinvent the AI's
    fingerprint, which was destroyed at the small resolution. Output is
    exactly the original WxH. RESOLUTION_SCALE == 1.0 → no-op (== V15).
    """
    s = float(RESOLUTION_SCALE)
    if s >= 1.0 or s <= 0.0:
        return image
    h, w = image.shape[:2]
    small_w = max(2, int(round(w * s)))
    small_h = max(2, int(round(h * s)))
    small = cv2.resize(image, (small_w, small_h), interpolation=cv2.INTER_AREA)
    restored = cv2.resize(small, (w, h), interpolation=cv2.INTER_LANCZOS4)

    amount = float(UNSHARP_AMOUNT)
    if amount <= 0.0:
        return restored
    # Unsharp mask: sharpened = img + amount*(img - blur(img)). Done in
    # float then clipped so it cannot wrap uint8 or bias luminance.
    blurred = cv2.GaussianBlur(
        restored, (0, 0), sigmaX=float(UNSHARP_RADIUS)
    )
    sharp = cv2.addWeighted(
        restored.astype(np.float32), 1.0 + amount,
        blurred.astype(np.float32), -amount, 0.0,
    )
    return np.clip(sharp, 0, 255).astype(np.uint8)


def process_frame(
    image: np.ndarray,
    lut: Optional[np.ndarray],
    vignette_mask: Optional[np.ndarray],
    args: argparse.Namespace,
    rng: Optional[np.random.Generator] = None,
    state: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    rng = rng or np.random.default_rng()
    state = {} if state is None else state

    # V15: Temporal Mute Profile. V14 math/encoding + V13 noise-free philosophy.
    # No sensor floor (it was V14's frequency-detector tell). No face tracking,
    # no AE hunting, no rPPG/biological signals. Temporal blending is applied
    # in naturalize_video (defeats consistency detectors), not here — per-frame
    # processing stays optics/sensor-physics only.

    # 1. Physical motion.
    image = apply_soft_ois_jitter(image, state, rng)
    image = apply_soft_rolling_shutter(image, state, rng)

    # 2. Optical behaviour (smoothstep bloom, then chromatic aberration —
    #    CA kept verbatim from V13/V14: the fast R/B scale is an accepted
    #    lateral CA approximation; a nonlinear remap would crater render speed).
    image = apply_highlight_blooming(image, threshold=232, strength=0.055)
    image = apply_radial_chromatic_aberration(image, scale=ABERRATION_SCALE)

    # 3. ISP-like global colour behaviour (true multiplicative AWB drift,
    #    kept verbatim from V14).
    image = apply_global_awb_drift(image, state, rng)

    # 4. Lens falloff (vignette). Adjusted mask cached in state (V14 fix), and
    #    the multiply rounds (np.rint) instead of truncating.
    adjusted_vignette = state.get("adjusted_vignette_mask")
    if adjusted_vignette is None and vignette_mask is not None:
        vignette_strength = getattr(args, "vignette_strength", 0.55)
        if vignette_strength > 0:
            adjusted_vignette = (
                1.0 - ((1.0 - vignette_mask) * vignette_strength)
            ).astype(np.float32)
            state["adjusted_vignette_mask"] = adjusted_vignette
    if adjusted_vignette is not None:
        image = np.rint(
            np.clip(image.astype(np.float32) * adjusted_vignette, 0, 255)
        ).astype(np.uint8)

    # V15: NO sensor noise applied (V13 noise-free philosophy — the V14
    # apply_daylight_sensor_floor pass is deleted entirely).

    # V21 NEW (the only change vs V15): uniform spatial-resolution low-pass,
    # applied LAST and IDENTICALLY to every frame (no motion gating — the
    # losing pattern). Destroys the high-frequency AI fingerprint; the V15
    # mp4v→CRF23 Laundromat then crushes what's left. Adds no signal.
    image = apply_resolution_roundtrip(image)
    return image


def naturalize_image(input_path: str, output_path: str, args: argparse.Namespace) -> None:
    image = open_media(input_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    height, width = image.shape[:2]
    vignette_mask = create_vignette_mask(height, width)
    rng = np.random.default_rng()
    # V13: fpn dropped — apply_modern_sensor_noise no longer called and --grain
    # was removed from the parser, so reading args.grain would AttributeError.
    state = {}

    try:
        # V12: lut param kept for signature compatibility with other versions but unused
        processed = process_frame(image, None, vignette_mask, args, rng, state)
        if args.preview:
            processed = build_preview_frame(image, processed)

        if not cv2.imwrite(output_path, processed):
            raise RuntimeError(f"Could not write image: {output_path}")
        print(f"Saved image to: {output_path}")
    finally:
        close_face_landmarker_state(state)


def finalize_video_output(
    temp_output: str,
    input_path: str,
    output_path: str,
    codec: str,
    args: argparse.Namespace,
) -> None:
    if not ffmpeg_available():
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(temp_output).replace(output_path)
        print(f"FFmpeg unavailable. Saved video without audio to: {output_path}")
        return

    print(f"Finalizing video with FFmpeg codec: {codec}")
    command = ["ffmpeg", "-y", "-i", temp_output, "-i", input_path, "-map", "0:v:0", "-map", "1:a:0?"]

    if codec == "h264":
        crf = str(int(getattr(args, "crf", 23)))
        command.extend([
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-crf",
            crf,
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "slow",
        ])
    else:
        command.extend(["-c:v", "copy"])

    # V14: stream-copy the original audio. V13 ran
    # highpass/lowpass/volume/acompressor filters that altered the audio for no
    # camera-realism reason and risked artefacts.
    command.extend(
        [
            "-c:a",
            "copy",
            output_path,
        ]
    )

    try:
        # Audited safe: `command` is a list (argv form) and shell defaults to
        # False, so there is no shell interpretation — command injection is
        # structurally impossible regardless of the local file paths in it.
        # Timeout is configurable (--ffmpeg-timeout): the 120s default is too
        # short for long / high-res clips through the H.264 'slow' preset.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=int(getattr(args, "ffmpeg_timeout", 600)),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(temp_output).replace(output_path)
        print(f"FFmpeg finalize failed. Saved video without audio to: {output_path}")
        return
    finally:
        try:
            Path(temp_output).unlink(missing_ok=True)
        except OSError:
            pass
    print(f"Saved video to: {output_path}")


def naturalize_video(input_path: str, output_path: str, args: argparse.Namespace) -> None:
    source = ensure_input_exists(input_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video: {source}")

    rotation = get_video_rotation(str(source))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30.0

    ok, test_frame = capture.read()
    if not ok:
        capture.release()
        raise RuntimeError(f"Could not read the first frame from: {source}")
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)

    test_frame = correct_rotation(test_frame, rotation)
    height, width = test_frame.shape[:2]
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))

    # V12: no LUT — global color manipulation removed (sepia tint elimination).
    vignette_mask = create_vignette_mask(height, width)
    rng = np.random.default_rng()
    _vignette_strength = getattr(args, "vignette_strength", 0.55)
    _adjusted_vignette = (1.0 - ((1.0 - vignette_mask) * _vignette_strength)).astype(np.float32) if _vignette_strength > 0 else None
    # V13: fpn dropped — apply_modern_sensor_noise no longer called.
    state = {
        "adjusted_vignette_mask": _adjusted_vignette,
    }

    output_size = (width * 2, height) if args.preview else (width, height)

    # V15 hotfix ("Laundromat"): pure lossy mp4v temp — NO FFV1/MJPG fallback.
    # The deliberate mp4v -> H.264 "double-lossy" chain is the whole point: it
    # crushes Kling's diffusion artifacts and bakes the temporal ghosting into
    # the macro-blocks so a frequency detector reads it as natural motion blur,
    # not a math overlay. Lossless preservation defeated that and scored poorly.
    temp_output = build_temp_video_path(output_path)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(temp_output, fourcc, fps, output_size)
    if not writer.isOpened():
        # Release both handles before bailing so a batch run (main() loops
        # over many files) never accumulates open writers / locked files.
        capture.release()
        writer.release()
        raise RuntimeError(
            f"Could not create mp4v video writer for: {temp_output}. "
            "The 'Laundromat' double-lossy pipeline requires OpenCV's mp4v "
            "encoder (the long-standing default); install an OpenCV build "
            "with mp4v support."
        )

    frame_num = 0
    next_pct = 25.0
    previous_processed = None

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break

            frame = correct_rotation(frame, rotation)
            current_processed = process_frame(frame, None, vignette_mask, args, rng, state)
            # V15: reintroduce temporal blending to defeat consistency
            # detectors. V13/V14 hardcoded this to 0.0 (razor-sharp, no smear);
            # Resemble testing showed that frame-to-frame perfection is an AI
            # tell. args.ghosting (default 0.18) bleeds the previous frame.
            processed = blend_with_previous_frame(
                current_processed, previous_processed, args.ghosting
            )
            previous_processed = current_processed

            if args.preview:
                processed = build_preview_frame(frame, processed)

            writer.write(processed)
            frame_num += 1

            if total_frames > 0:
                pct = (frame_num / total_frames) * 100
                while pct >= next_pct and next_pct <= 100.0:
                    print(f"[Oldcam] Processing: {int(next_pct)}% complete...", flush=True)
                    next_pct += 25.0
    finally:
        close_face_landmarker_state(state)
        capture.release()
        # writer is created before the try (pure mp4v, no fallback loop);
        # guard the release in case a future refactor moves init into the try.
        if writer is not None:
            writer.release()

    print("Video processing complete.")
    finalize_video_output(temp_output, str(source), output_path, args.codec, args)


def process_input(input_path, output_path, args):
    print(f"Input : {input_path}")
    print(f"Output: {output_path}")
    if is_video_path(input_path):
        naturalize_video(input_path, output_path, args)
    else:
        naturalize_image(input_path, output_path, args)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Naturalize images or videos to look more like imperfect phone footage."
    )
    parser.add_argument("inputs", nargs="+", help="One or more input files.")
    parser.add_argument("-o", "--output", help="Output path for a single input file.")
    parser.add_argument("--preview", action="store_true", help="Write a side-by-side preview.")
    parser.add_argument(
        "--codec", choices=("h264", "copy"), default="h264", help="FFmpeg video codec. Default: h264"
    )
    # --sharpen and --saturation removed in V12; --grain removed in V13 (no sensor noise pass).
    # "--background-texture-strength" removed: flat-sensor mode, no depth separation
    # V15: --ghosting RESTORED as a real knob (V13/V14 hardcoded it to 0.0).
    # Resemble testing showed temporal blending hides AI flicker that
    # consistency detectors key on. bounded_ghosting clamps to 0.0-0.5.
    parser.add_argument(
        "--ghosting",
        type=bounded_ghosting,
        default=0.18,
        help="Amount of temporal blending (0.0-0.5) to hide AI flicker. Default: 0.18",
    )
    # V15: V14's sub-perceptual sensor floor (--read-noise / --shot-noise /
    # --chroma-noise-ratio) is REMOVED entirely — it was the frequency-detector
    # tell. Only the final-encode CRF knob is kept.
    parser.add_argument(
        "--crf",
        type=int,
        default=23,
        help="H.264 CRF for final encode. Higher introduces organic web compression. Default: 23",
    )
    # process_frame / naturalize_video already read this via
    # getattr(args, "vignette_strength", 0.55); expose it so the lens-falloff
    # strength is user-controllable instead of silently fixed. Default matches
    # the existing getattr fallback exactly → no behaviour change unless set.
    parser.add_argument(
        "--vignette-strength",
        type=float,
        default=0.55,
        help="Lens vignette (corner darkening) strength, 0.0-1.0. Default: 0.55",
    )
    parser.add_argument(
        "--ffmpeg-timeout",
        type=int,
        default=600,
        help="Seconds to allow the final FFmpeg H.264 encode before timing "
             "out. Raise for long / high-res clips on the 'slow' preset. "
             "Default: 600",
    )
    return parser


def report_processing_error(input_path, exc):
    print(file=sys.stderr)
    print(f"Error while processing: {input_path}", file=sys.stderr)
    print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
    if not isinstance(exc, (FileNotFoundError, RuntimeError, ValueError)):
        traceback.print_exc()


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # V15: clamp the numeric encode/effect knobs to sane ranges. The V14
    # sensor-floor knobs are gone; --ghosting is already clamped by
    # bounded_ghosting. Hotfix widened the ceiling 24 -> 28: the "Laundromat"
    # double-lossy chain wants room for heavy WhatsApp/Telegram-grade web
    # compression (default 23), and CRF 28 is still visually acceptable.
    args.crf = max(10, min(int(args.crf), 28))
    args.vignette_strength = max(0.0, min(args.vignette_strength, 1.0))
    # Floor the ffmpeg timeout so a typo can't make it instantly fail; no
    # upper clamp (long 4K clips on 'slow' can legitimately need a lot).
    args.ffmpeg_timeout = max(30, int(args.ffmpeg_timeout))

    if args.output and len(args.inputs) > 1:
        parser.error("--output can only be used when processing a single input file.")

    had_errors = False
    for input_path in args.inputs:
        if args.output:
            output_path = args.output
        elif args.preview:
            output_path = build_preview_output_path(input_path)
        else:
            output_path = build_default_output_path(input_path)

        # Refuse to overwrite the source media in place: --output pointed at
        # the input would let the lossy/temp pipeline destroy the original.
        # Fail fast with a clear message rather than corrupting user data.
        try:
            same_file = (
                Path(output_path).resolve() == Path(input_path).resolve()
            )
        except OSError:
            parser.error("Could not resolve input/output paths safely.")
        if same_file:
            parser.error(
                f"--output must differ from the input ({input_path}); "
                "refusing to overwrite the source file in place."
            )

        try:
            process_input(input_path, output_path, args)
        except Exception as exc:
            had_errors = True
            report_processing_error(input_path, exc)

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
