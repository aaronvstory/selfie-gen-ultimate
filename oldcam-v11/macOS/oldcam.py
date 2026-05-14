#!/usr/bin/env python3
"""
oldcam.py - V11 "Spatial Sync + AWB Drift" Virtual Hardware Simulator

Optimized for modern handheld selfie videos. Prioritizes temporal camera
behavior: OIS micro-jitter, random velocity rolling shutter, chroma sensor
noise, and H.264 motion compression.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision
import numpy as np

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}
ABERRATION_SCALE = 0.0015
TASK_MODEL_FILENAME = "face_landmarker.task"


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
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}-oldcam-v11{path.suffix}"))


def build_preview_output_path(input_path):
    path = Path(input_path)
    return str(path.with_name(f"{path.stem}-preview-v11{path.suffix}"))


def build_temp_video_path(output_path):
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
        "Oldcam V11",
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


def get_dynamic_region_masks(image, state):
    """Extracts temporally-stabilized, optically blended boolean masks for focal regions."""
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


def apply_highlight_blooming(image, threshold=220, strength=0.2):
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    highlights = cv2.bitwise_and(image, image, mask=mask)

    small = cv2.resize(
        highlights, (max(1, w // 8), max(1, h // 8)), interpolation=cv2.INTER_LINEAR
    )
    blurred = cv2.GaussianBlur(small, (15, 15), 0)
    bloom = cv2.resize(blurred, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.addWeighted(image, 1.0, bloom, strength, 0)


def apply_dynamic_tone_mapping(image):
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    l_channel, a_channel, b_channel = cv2.split(lab)
    cl = cv2.createCLAHE(clipLimit=1.2, tileGridSize=(8, 8)).apply(l_channel)
    return cv2.cvtColor(cv2.merge((cl, a_channel, b_channel)), cv2.COLOR_LAB2BGR)


def get_temporal_noise_field(state, shape, rng, strength=1.0, key="temporal_noise"):
    previous = state.get(key)
    fresh = rng.normal(0.0, strength, shape).astype(np.float32)
    if previous is None or previous.shape != shape:
        field = fresh
    else:
        field = previous * 0.85 + fresh * 0.15
    state[key] = field
    return field


def apply_modern_sensor_noise(image, grain, rng, state=None, fpn_mask=None):
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


def close_face_landmarker_state(state):
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
    drift = float(state.get("awb_drift", 0.0))
    drift += float(rng.normal(0.0, 0.05))
    drift = float(np.clip(drift, -1.5, 1.5))
    state["awb_drift"] = drift
    image_f = image.astype(np.float32)
    image_f += drift
    return np.clip(image_f, 0, 255).astype(np.uint8)


SPATIAL_PHASE_OFFSETS = {"forehead": 0.0, "left_cheek": 0.15, "right_cheek": 0.15, "chin": 0.25}


def synchronize_base_frequency(image, state, face_mask, fps=30.0):
    """Extracts existing temporal frequency from focal region to sync textures."""
    if not state.get("face_detected", False):
        return 1.2

    history = state.get("g_history", [])
    mean_g = cv2.mean(image[:, :, 1], mask=(face_mask[:, :, 0] > 0).astype(np.uint8))[0]
    history.append(mean_g)
    if len(history) > int(fps * 3):
        history.pop(0)
    state["g_history"] = history

    target_hz = state.get("target_hz", 1.2)
    if len(history) >= 60 and state.get("frame_count", 0) % 30 == 0:
        sig = np.array(history, dtype=np.float32)
        sig = sig - np.mean(sig)
        freqs = np.fft.rfftfreq(len(sig), d=1.0 / fps)
        mags = np.abs(np.fft.rfft(sig))
        valid = (freqs >= 0.8) & (freqs <= 1.8)
        if np.any(valid):
            target_hz = float(freqs[valid][np.argmax(mags[valid])])
            state["target_hz"] = target_hz
    return target_hz


def apply_synchronized_spatial_fluctuation(image, state, region_masks, target_hz, fps=30.0):
    """Applies spatially-delayed fluctuations synchronized to detected base frequency."""
    frame_count = state.get("frame_count", 0)
    state["frame_count"] = frame_count + 1
    t = frame_count / fps
    envelope = 1.0 + 0.15 * np.sin(t * 2 * np.pi * 0.3)

    ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
    warm_mask = cv2.inRange(ycrcb, np.array([0, 133, 77]), np.array([255, 173, 127])) / 255.0
    warm_mask = np.stack([warm_mask] * 3, axis=-1).astype(np.float32)
    img_float = image.astype(np.float32)

    for region, mask in region_masks.items():
        offset = SPATIAL_PHASE_OFFSETS.get(region, 0.0)
        phase = (t * 2 * np.pi * target_hz) + offset
        shift_intensity = np.sin(phase) * envelope * 0.45
        combined_mask = mask * warm_mask
        img_float[:, :, 1] += shift_intensity * 1.0 * combined_mask[:, :, 0]
        img_float[:, :, 2] += shift_intensity * 0.45 * combined_mask[:, :, 0]
        img_float[:, :, 0] -= shift_intensity * 0.10 * combined_mask[:, :, 0]

    return img_float.clip(0, 255).astype(np.uint8)


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


def blend_with_previous_frame(current_frame, previous_frame, ghosting):
    if previous_frame is None or ghosting <= 0.0:
        return current_frame
    return cv2.addWeighted(current_frame, 1.0 - ghosting, previous_frame, ghosting, 0)


def process_frame(image, lut, vignette_mask, args, rng=None, state=None):
    rng = rng or np.random.default_rng()
    state = {} if state is None else state
    h, w = image.shape[:2]

    region_masks = get_dynamic_region_masks(image, state)
    full_face_mask = state.get("full_face_mask", np.zeros((h, w, 3), dtype=np.float32))

    if state.get("face_detected", False) and region_masks:
        target_hz = synchronize_base_frequency(image, state, full_face_mask)
        image = apply_synchronized_spatial_fluctuation(image, state, region_masks, target_hz)

    image = apply_global_awb_drift(image, state, rng)
    image = apply_soft_ois_jitter(image, state, rng)
    image = apply_soft_rolling_shutter(image, state, rng)
    image = apply_subtle_af_breathing(image, state, rng)
    # image = apply_dynamic_relighting(image, state)
    image = apply_ae_stepping(image, state)
    image = apply_dynamic_tone_mapping(image)
    image = apply_highlight_blooming(image, threshold=232, strength=0.055)

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * getattr(args, "saturation", 1.02), 0, 255)
    image = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    image = cv2.LUT(image, create_neutral_phone_lut() if lut is None else lut)
    image = apply_modern_sensor_noise(image, getattr(args, "grain", 1.0), rng, state=state, fpn_mask=state.get("fpn"))
    image = apply_radial_chromatic_aberration(image, scale=0.0006)
    # image = apply_soft_background_texture(image, full_face_mask, strength=getattr(args, "background_texture_strength", 0.08))

    adjusted_vignette = state.get("adjusted_vignette_mask")
    if adjusted_vignette is None and vignette_mask is not None:
        vignette_strength = getattr(args, "vignette_strength", 0.55)
        if vignette_strength > 0:
            adjusted_vignette = (1.0 - ((1.0 - vignette_mask) * vignette_strength)).astype(np.float32)
    if adjusted_vignette is not None:
        image = np.clip(image.astype(np.float32) * adjusted_vignette, 0, 255).astype(np.uint8)

    return image


def naturalize_image(input_path, output_path, args):
    image = open_media(input_path)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    height, width = image.shape[:2]
    lut = create_neutral_phone_lut()
    vignette_mask = create_vignette_mask(height, width)
    rng = np.random.default_rng()
    state = {"fpn": rng.normal(0.0, args.grain * 1.2, (height, width, 3)).astype(np.float32)}

    try:
        processed = process_frame(image, lut, vignette_mask, args, rng, state)
        if args.preview:
            processed = build_preview_frame(image, processed)

        if not cv2.imwrite(output_path, processed):
            raise RuntimeError(f"Could not write image: {output_path}")
        print(f"Saved image to: {output_path}")
    finally:
        close_face_landmarker_state(state)


def finalize_video_output(temp_output, input_path, output_path, codec):
    if not ffmpeg_available():
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(temp_output).replace(output_path)
        print(f"FFmpeg unavailable. Saved video without audio to: {output_path}")
        return

    print(f"Finalizing video with FFmpeg codec: {codec}")
    command = ["ffmpeg", "-y", "-i", temp_output, "-i", input_path, "-map", "0:v:0", "-map", "1:a:0?"]

    if codec == "h264":
        command.extend([
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "slow",
        ])
    else:
        command.extend(["-c:v", "copy"])

    command.extend(
        [
            "-c:a",
            "aac",
            "-af",
            "highpass=f=300,lowpass=f=4000,volume=8dB,acompressor=threshold=0.1:ratio=10",
            output_path,
        ]
    )

    try:
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)
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


def naturalize_video(input_path, output_path, args):
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

    lut = create_neutral_phone_lut()
    vignette_mask = create_vignette_mask(height, width)
    rng = np.random.default_rng()
    _vignette_strength = getattr(args, "vignette_strength", 0.55)
    _adjusted_vignette = (1.0 - ((1.0 - vignette_mask) * _vignette_strength)).astype(np.float32) if _vignette_strength > 0 else None
    state = {
        "fpn": rng.normal(0.0, args.grain * 1.2, (height, width, 3)).astype(np.float32),
        "adjusted_vignette_mask": _adjusted_vignette,
    }

    temp_output = build_temp_video_path(output_path)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    output_size = (width * 2, height) if args.preview else (width, height)
    writer = cv2.VideoWriter(temp_output, fourcc, fps, output_size)
    if not writer.isOpened():
        capture.release()
        raise RuntimeError(f"Could not create video writer for: {temp_output}")

    frame_num = 0
    next_pct = 25.0
    previous_processed = None

    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break

            frame = correct_rotation(frame, rotation)
            current_processed = process_frame(frame, lut, vignette_mask, args, rng, state)
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
        writer.release()

    print("Video processing complete.")
    finalize_video_output(temp_output, str(source), output_path, args.codec)


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
    parser.add_argument(
        "--sharpen", type=float, default=0.8, help="Sharpening blur radius. Default: 0.8"
    )
    parser.add_argument(
        "--saturation", type=float, default=1.12, help="Saturation multiplier. Default: 1.12"
    )
    parser.add_argument("--grain", type=int, default=1, help="Sensor-grain strength. Default: 1")
    # "--background-texture-strength" removed: flat-sensor mode, no depth separation
    parser.add_argument(
        "--ghosting",
        type=bounded_ghosting,
        default=0.08,
        help="Blend 0.0-0.5 of previous frame. Default: 0.08",
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

        try:
            process_input(input_path, output_path, args)
        except Exception as exc:
            had_errors = True
            report_processing_error(input_path, exc)

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
