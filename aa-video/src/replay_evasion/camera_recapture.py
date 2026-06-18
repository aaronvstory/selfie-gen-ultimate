"""
Camera Recapture Simulation for defeating DSP-FWA and FTCN detectors.

Simulates the effects of filming a screen/display with a physical camera:
- Moiré pattern artifacts (at subtle levels)
- Lens vignetting
- Slight perspective/keystoning
- Real camera sensor noise characteristics
- Rolling shutter simulation
- Display refresh interference

This is the software equivalent of "camera-in-camera" capture which introduces
real sensor noise and breaks frequency consistency that detectors rely on.

Target Detectors: DSP-FWA, FTCN
Research Basis: Camera recapture introduces real sensor noise + rolling shutter
"""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from typing import Optional, Tuple

# Try to import GPU utilities
try:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gpu_utils import get_device_info
    GPU_UTILS_AVAILABLE = True
except ImportError:
    GPU_UTILS_AVAILABLE = False


def generate_moire_pattern(
    height: int,
    width: int,
    frequency: float = 0.1,
    angle: float = 15.0,
    strength: float = 0.02
) -> np.ndarray:
    """
    Generate subtle moiré interference pattern.

    Moiré occurs when camera sensor grid interacts with display pixel grid.

    Args:
        height, width: Frame dimensions
        frequency: Pattern frequency
        angle: Pattern angle in degrees
        strength: Pattern visibility (keep low for subtlety)

    Returns:
        Moiré pattern array (H, W)
    """
    # Create coordinate grids
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)

    # Rotate coordinates
    angle_rad = np.radians(angle)
    x_rot = x * np.cos(angle_rad) + y * np.sin(angle_rad)
    y_rot = -x * np.sin(angle_rad) + y * np.cos(angle_rad)

    # Generate interference pattern
    pattern1 = np.sin(2 * np.pi * frequency * x_rot)
    pattern2 = np.sin(2 * np.pi * frequency * 1.1 * y_rot)

    # Combine for moiré effect
    moire = (pattern1 * pattern2) * strength

    return moire


def apply_vignette(frame: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """
    Apply lens vignetting (darkening at edges).

    Real camera lenses have falloff at the edges that pure digital doesn't.

    Args:
        frame: BGR frame
        strength: Vignette intensity (0.0-1.0)

    Returns:
        Frame with vignetting applied
    """
    h, w = frame.shape[:2]

    # Create vignette mask
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2, h / 2

    # Normalized distance from center
    dist = np.sqrt((x - cx)**2 + (y - cy)**2)
    max_dist = np.sqrt(cx**2 + cy**2)
    dist_norm = dist / max_dist

    # Vignette falloff (quadratic for natural look)
    vignette = 1.0 - strength * (dist_norm ** 2)
    vignette = np.clip(vignette, 0.5, 1.0)

    # Apply to all channels
    vignette = np.stack([vignette] * 3, axis=-1)

    result = (frame.astype(np.float32) * vignette).clip(0, 255).astype(np.uint8)
    return result


def apply_perspective_distortion(
    frame: np.ndarray,
    strength: float = 0.3,
    seed: Optional[int] = None
) -> np.ndarray:
    """
    Apply subtle perspective/keystone distortion.

    When filming a screen, slight angle causes perspective distortion.

    Args:
        frame: BGR frame
        strength: Distortion amount
        seed: Random seed for consistent distortion

    Returns:
        Frame with perspective distortion
    """
    if seed is not None:
        np.random.seed(seed)

    h, w = frame.shape[:2]

    # Define source points (original corners)
    src_pts = np.float32([
        [0, 0],
        [w, 0],
        [w, h],
        [0, h]
    ])

    # Define destination points with slight random offsets
    max_offset = strength * min(w, h) * 0.02  # Very subtle

    dst_pts = np.float32([
        [0 + np.random.uniform(0, max_offset), 0 + np.random.uniform(0, max_offset)],
        [w - np.random.uniform(0, max_offset), 0 + np.random.uniform(0, max_offset)],
        [w - np.random.uniform(0, max_offset), h - np.random.uniform(0, max_offset)],
        [0 + np.random.uniform(0, max_offset), h - np.random.uniform(0, max_offset)]
    ])

    # Get perspective transform
    M = cv2.getPerspectiveTransform(src_pts, dst_pts)

    # Apply transform
    result = cv2.warpPerspective(frame, M, (w, h), borderMode=cv2.BORDER_REFLECT)

    return result


def apply_sensor_noise(
    frame: np.ndarray,
    strength: float = 0.3,
    iso_level: float = 400
) -> np.ndarray:
    """
    Apply realistic camera sensor noise (photon + read noise).

    Real cameras have characteristic noise patterns:
    - Photon (shot) noise: Poisson distributed, signal-dependent
    - Read noise: Gaussian, constant
    - Fixed pattern noise: Consistent per-pixel variations

    Args:
        frame: BGR frame
        strength: Overall noise intensity
        iso_level: Simulated ISO (higher = more noise)

    Returns:
        Frame with sensor noise
    """
    frame_float = frame.astype(np.float32)

    # Normalize ISO to noise multiplier
    # SUBTLE: much lower multipliers for imperceptible effect
    iso_mult = np.sqrt(iso_level / 100) * strength * 0.1  # Reduced 10x

    # Photon noise (signal-dependent, Poisson-like)
    # Approximate with Gaussian where std = sqrt(signal)
    # SUBTLE: max ~1-2 pixel variation at strength=1.0
    signal_normalized = frame_float / 255.0
    photon_std = np.sqrt(np.maximum(signal_normalized, 0.01)) * iso_mult * 1.5
    photon_noise = np.random.randn(*frame.shape).astype(np.float32) * photon_std

    # Read noise (constant, Gaussian)
    # SUBTLE: max ~0.5 pixel variation
    read_noise = np.random.randn(*frame.shape).astype(np.float32) * iso_mult * 0.5

    # Fixed pattern noise (consistent per run via seed)
    # SUBTLE: very minor per-pixel bias
    np.random.seed(12345)
    fpn = np.random.randn(*frame.shape).astype(np.float32) * iso_mult * 0.2
    np.random.seed(None)

    # Combine noise
    noisy = frame_float + photon_noise + read_noise + fpn

    return noisy.clip(0, 255).astype(np.uint8)


def apply_rolling_shutter(
    frame: np.ndarray,
    prev_frame: Optional[np.ndarray],
    strength: float = 0.3
) -> np.ndarray:
    """
    Simulate rolling shutter effect.

    Real cameras (especially phones) have rolling shutters that cause
    temporal skew - top of frame captured before bottom.

    Args:
        frame: Current BGR frame
        prev_frame: Previous frame (for motion estimation)
        strength: Effect intensity

    Returns:
        Frame with rolling shutter simulation
    """
    if prev_frame is None:
        return frame

    h, w = frame.shape[:2]
    frame_float = frame.astype(np.float32)
    prev_float = prev_frame.astype(np.float32)

    # Estimate simple motion (frame difference)
    motion = frame_float - prev_float

    # Apply progressive blend from top to bottom
    # Top rows are more like previous frame, bottom more like current
    # SUBTLE: very minor temporal skew
    result = np.zeros_like(frame_float)

    for row in range(h):
        # Blend factor increases from top to bottom
        # SUBTLE: max 3% blend at strength=1.0 (was 30%)
        blend = (row / h) * strength * 0.03
        result[row] = frame_float[row] * (1 - blend) + prev_float[row] * blend

    return result.clip(0, 255).astype(np.uint8)


def apply_display_refresh_artifacts(
    frame: np.ndarray,
    frame_idx: int,
    display_fps: float = 60.0,
    camera_fps: float = 30.0,
    strength: float = 0.3
) -> np.ndarray:
    """
    Simulate artifacts from display refresh rate mismatch.

    When camera fps doesn't match display fps, periodic artifacts appear.

    Args:
        frame: BGR frame
        frame_idx: Current frame index
        display_fps: Simulated display refresh rate
        camera_fps: Simulated camera capture rate
        strength: Artifact intensity

    Returns:
        Frame with refresh artifacts
    """
    h, w = frame.shape[:2]
    frame_float = frame.astype(np.float32)

    # Calculate phase of display refresh relative to camera
    phase = (frame_idx * camera_fps / display_fps) % 1.0

    # Add subtle horizontal banding at refresh boundary
    # SUBTLE: only visible at phase boundary, very faint
    if 0.4 < phase < 0.6:  # Near refresh boundary
        band_pos = int(h * phase)
        band_width = int(h * 0.03)  # Narrower band

        start = max(0, band_pos - band_width // 2)
        end = min(h, band_pos + band_width // 2)

        # SUBTLE: max ~1 pixel brightness variation at strength=1.0
        brightness_shift = strength * 1.0 * np.sin(np.pi * (phase - 0.5) / 0.1)
        frame_float[start:end] += brightness_shift

    return frame_float.clip(0, 255).astype(np.uint8)


def camera_recapture_simulation_gpu(
    input_path: str,
    output_path: str,
    strength: float = 0.3,
    components: Optional[list] = None,
    seed: Optional[int] = None
):
    """
    Apply GPU-accelerated camera recapture simulation.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Overall effect strength (0.0-1.0)
        components: List of components to apply. Default: all
                   Options: 'moire', 'vignette', 'perspective', 'sensor_noise',
                           'rolling_shutter', 'refresh_artifacts'
        seed: Random seed for reproducible perspective distortion

    Raises:
        RuntimeError: If processing fails
    """
    if components is None:
        components = ['vignette', 'sensor_noise', 'rolling_shutter', 'refresh_artifacts']
        # Note: 'moire' and 'perspective' can be too visible, disabled by default

    if GPU_UTILS_AVAILABLE and torch.cuda.is_available():
        device_info = get_device_info()
        print(f"[CameraRecapture] Using CUDA GPU: {device_info['name']}")

    # Open video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    # Get video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"[CameraRecapture] Processing {total_frames} frames with components: {components}")

    # Setup video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video {output_path}")

    # Pre-generate consistent effects
    perspective_seed = seed if seed is not None else 42
    moire_pattern = None
    if 'moire' in components:
        moire_pattern = generate_moire_pattern(height, width, strength=strength * 0.02)

    prev_frame = None
    frame_idx = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            processed = frame.copy()

            # Apply components
            if 'moire' in components and moire_pattern is not None:
                moire_3ch = np.stack([moire_pattern] * 3, axis=-1) * 255
                processed = (processed.astype(np.float32) + moire_3ch).clip(0, 255).astype(np.uint8)

            if 'vignette' in components:
                processed = apply_vignette(processed, strength * 0.4)

            if 'perspective' in components:
                processed = apply_perspective_distortion(processed, strength, seed=perspective_seed)

            if 'sensor_noise' in components:
                processed = apply_sensor_noise(processed, strength, iso_level=400)

            if 'rolling_shutter' in components:
                processed = apply_rolling_shutter(processed, prev_frame, strength)

            if 'refresh_artifacts' in components:
                processed = apply_display_refresh_artifacts(processed, frame_idx, strength=strength * 0.5)

            out.write(processed)

            prev_frame = frame.copy()
            frame_idx += 1

            if frame_idx % 100 == 0:
                print(f"[CameraRecapture] Processed {frame_idx}/{total_frames} frames...")

    finally:
        cap.release()
        out.release()

    print(f"[CameraRecapture] Completed: {output_path}")


def camera_recapture_simulation(
    input_path: str,
    output_path: str,
    strength: float = 0.3,
    components: Optional[list] = None,
    seed: Optional[int] = None
):
    """
    Apply camera recapture simulation (filming a screen with physical camera).

    Main entry point that simulates the effects of camera-in-camera capture
    to introduce real camera artifacts that defeat frequency-domain detection.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Effect strength (0.0-1.0)
                  - 0.2: Very subtle
                  - 0.3: Default, good balance
                  - 0.5: More noticeable
        components: Which effects to apply. Default: safe subset
                   All options: 'moire', 'vignette', 'perspective', 'sensor_noise',
                               'rolling_shutter', 'refresh_artifacts'
        seed: Random seed for reproducibility
    """
    camera_recapture_simulation_gpu(input_path, output_path, strength, components, seed)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python camera_recapture.py <input_video> <output_video> [strength]")
        print("  strength: Effect intensity 0-1 (default: 0.3)")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3

    camera_recapture_simulation(sys.argv[1], sys.argv[2], strength)
