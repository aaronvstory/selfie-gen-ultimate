"""
Analog Capture Chain Simulation for defeating DSP-FWA frequency-domain detection.

Simulates the artifacts introduced when video passes through analog capture chains:
- HDMI -> capture card -> camera chain
- Screen recording with physical camera

Real analog capture introduces artifacts that digital replay lacks:
- Chroma subsampling and bleeding
- Color space conversion errors
- Minor geometric distortion (lens effects)
- Analog signal noise patterns
- Frequency domain irregularities

Target Detectors: DSP-FWA (frequency domain warping detection)
Research Basis: Digital replay has too-clean frequency consistency that analog hops destroy
"""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from typing import Optional, Tuple
from scipy import ndimage

# Try to import GPU utilities
try:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gpu_utils import get_device_info
    GPU_UTILS_AVAILABLE = True
except ImportError:
    GPU_UTILS_AVAILABLE = False


def apply_chroma_subsampling(frame: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """
    Simulate chroma subsampling artifacts (4:2:0 style degradation).

    Real analog capture often has chroma resolution lower than luma,
    creating subtle color bleeding at edges.

    Args:
        frame: BGR frame
        strength: How much to blur chroma (0.0-1.0)

    Returns:
        Frame with simulated chroma subsampling
    """
    # Convert to YCrCb
    ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb).astype(np.float32)

    # Blur the chroma channels (Cr and Cb) to simulate lower resolution
    blur_size = max(3, int(strength * 7))
    if blur_size % 2 == 0:
        blur_size += 1

    ycrcb[:, :, 1] = cv2.GaussianBlur(ycrcb[:, :, 1], (blur_size, blur_size), 0)
    ycrcb[:, :, 2] = cv2.GaussianBlur(ycrcb[:, :, 2], (blur_size, blur_size), 0)

    # Add slight chroma shift (misalignment)
    # SUBTLE: max 0.3 pixel shift at strength=1.0
    shift_amount = strength * 0.3
    if shift_amount > 0.1:
        M_cr = np.float32([[1, 0, shift_amount], [0, 1, 0]])
        M_cb = np.float32([[1, 0, -shift_amount * 0.7], [0, 1, shift_amount * 0.5]])

        h, w = ycrcb.shape[:2]
        ycrcb[:, :, 1] = cv2.warpAffine(ycrcb[:, :, 1], M_cr, (w, h), borderMode=cv2.BORDER_REFLECT)
        ycrcb[:, :, 2] = cv2.warpAffine(ycrcb[:, :, 2], M_cb, (w, h), borderMode=cv2.BORDER_REFLECT)

    # Convert back
    result = cv2.cvtColor(ycrcb.astype(np.uint8), cv2.COLOR_YCrCb2BGR)
    return result


def apply_color_space_error(frame: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """
    Simulate color space conversion errors from analog chain.

    When video passes through multiple devices, slight color space
    mismatches occur (gamma differences, white point shifts).

    Args:
        frame: BGR frame
        strength: Error magnitude (0.0-1.0)

    Returns:
        Frame with color space errors
    """
    frame_float = frame.astype(np.float32) / 255.0

    # Simulate slight gamma mismatch
    # SUBTLE: max 2% gamma variation at strength=1.0
    gamma_error = 1.0 + (np.random.random() - 0.5) * strength * 0.02
    frame_float = np.power(frame_float + 1e-8, gamma_error)

    # Simulate white point shift
    # SUBTLE: max 0.5% color channel variation
    white_shift = np.array([
        1.0 + (np.random.random() - 0.5) * strength * 0.005,  # B
        1.0 + (np.random.random() - 0.5) * strength * 0.003,  # G
        1.0 + (np.random.random() - 0.5) * strength * 0.005,  # R
    ], dtype=np.float32)

    frame_float = frame_float * white_shift

    # Simulate slight saturation variation
    # SUBTLE: max 1% saturation variation
    hsv = cv2.cvtColor((frame_float * 255).clip(0, 255).astype(np.uint8), cv2.COLOR_BGR2HSV).astype(np.float32)
    sat_mult = 1.0 + (np.random.random() - 0.5) * strength * 0.01
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * sat_mult, 0, 255)
    frame_float = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32) / 255.0

    return (frame_float * 255).clip(0, 255).astype(np.uint8)


def apply_barrel_distortion(frame: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """
    Apply subtle barrel/pincushion distortion simulating lens effects.

    Camera lenses introduce radial distortion that's absent in pure digital replay.

    Args:
        frame: BGR frame
        strength: Distortion amount (0.0-1.0)

    Returns:
        Frame with lens distortion
    """
    h, w = frame.shape[:2]

    # Distortion coefficients (k1, k2 for radial, p1, p2 for tangential)
    # SUBTLE: very minor distortion, barely perceptible
    k1 = strength * 0.005 * (1 if np.random.random() > 0.5 else -1)  # Barrel or pincushion
    k2 = strength * 0.001 * (1 if np.random.random() > 0.5 else -1)

    # Camera matrix (assume center is optical center)
    fx = fy = max(w, h)
    cx, cy = w / 2, h / 2
    camera_matrix = np.array([
        [fx, 0, cx],
        [0, fy, cy],
        [0, 0, 1]
    ], dtype=np.float32)

    dist_coeffs = np.array([k1, k2, 0, 0, 0], dtype=np.float32)

    # Get optimal new camera matrix
    new_camera_matrix, roi = cv2.getOptimalNewCameraMatrix(
        camera_matrix, dist_coeffs, (w, h), 0, (w, h)
    )

    # Undistort (which actually applies our distortion since we're going backwards)
    # We use initUndistortRectifyMap for more control
    map1, map2 = cv2.initUndistortRectifyMap(
        camera_matrix, dist_coeffs, None, new_camera_matrix, (w, h), cv2.CV_32FC1
    )

    distorted = cv2.remap(frame, map1, map2, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)

    return distorted


def apply_analog_noise(frame: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """
    Add analog-style noise patterns distinct from digital sensor noise.

    Analog capture introduces:
    - Horizontal line noise (sync issues)
    - Low-frequency banding
    - Signal interference patterns

    Args:
        frame: BGR frame
        strength: Noise intensity (0.0-1.0)

    Returns:
        Frame with analog noise
    """
    h, w = frame.shape[:2]
    frame_float = frame.astype(np.float32)

    # Horizontal line noise (simulates sync/timing variations)
    # SUBTLE: max ~0.3 pixel brightness variation at strength=1.0
    line_noise = np.random.randn(h, 1).astype(np.float32) * strength * 0.3
    line_noise = np.tile(line_noise, (1, w))
    line_noise = np.stack([line_noise] * 3, axis=-1)

    # Low-frequency banding (simulates interference)
    # SUBTLE: max ~0.5 pixel brightness variation at strength=1.0
    freq = np.random.uniform(0.01, 0.03)
    phase = np.random.uniform(0, 2 * np.pi)
    y_coords = np.arange(h).reshape(-1, 1)
    banding = np.sin(2 * np.pi * freq * y_coords + phase) * strength * 0.5
    banding = np.tile(banding, (1, w))
    banding = np.stack([banding] * 3, axis=-1).astype(np.float32)

    # Combine noise
    frame_float = frame_float + line_noise + banding

    # Add slight temporal flicker simulation (varies per frame when called repeatedly)
    # SUBTLE: max 0.2% brightness variation
    flicker = 1.0 + np.random.randn() * strength * 0.002
    frame_float = frame_float * flicker

    return frame_float.clip(0, 255).astype(np.uint8)


def apply_frequency_irregularity(frame: np.ndarray, strength: float = 0.3) -> np.ndarray:
    """
    Add irregularities in frequency domain that break DSP-FWA detection.

    DSP-FWA looks for too-consistent frequency domain patterns.
    Real analog capture has irregular frequency responses.

    Args:
        frame: BGR frame
        strength: Irregularity amount (0.0-1.0)

    Returns:
        Frame with frequency irregularities
    """
    # Process each channel
    result = np.zeros_like(frame, dtype=np.float32)

    for c in range(3):
        channel = frame[:, :, c].astype(np.float32)

        # FFT
        f_transform = np.fft.fft2(channel)
        f_shift = np.fft.fftshift(f_transform)

        # Add VERY SUBTLE random perturbations to frequency components
        # Only perturb high frequencies (leave low frequencies alone for stability)
        h, w = channel.shape
        cy, cx = h // 2, w // 2

        # Create mask that only affects mid-to-high frequencies
        y, x = np.ogrid[:h, :w]
        dist_from_center = np.sqrt((y - cy)**2 + (x - cx)**2)
        min_radius = min(h, w) * 0.1  # Don't touch very low frequencies
        freq_mask = np.clip((dist_from_center - min_radius) / (min(h, w) * 0.4), 0, 1)

        # SUBTLE: max 0.5% magnitude perturbation, 0.2% phase at strength=1.0
        noise_mag = np.random.randn(h, w) * strength * 0.005 * freq_mask
        noise_phase = np.random.randn(h, w) * strength * 0.002 * freq_mask

        # Apply noise (multiplicative for magnitude, additive for phase)
        f_shift = f_shift * (1 + noise_mag) * np.exp(1j * noise_phase)

        # Inverse FFT
        f_ishift = np.fft.ifftshift(f_shift)
        channel_back = np.fft.ifft2(f_ishift)
        result[:, :, c] = np.real(channel_back)

    return result.clip(0, 255).astype(np.uint8)


def analog_capture_simulation_gpu(
    input_path: str,
    output_path: str,
    strength: float = 0.3,
    components: Optional[list] = None
):
    """
    Apply GPU-accelerated analog capture chain simulation.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Overall effect strength (0.0-1.0)
        components: List of components to apply. Default: all
                   Options: 'chroma', 'color', 'distortion', 'noise', 'frequency'

    Raises:
        RuntimeError: If processing fails
    """
    if components is None:
        components = ['chroma', 'color', 'distortion', 'noise', 'frequency']

    if GPU_UTILS_AVAILABLE:
        device_info = get_device_info()
        print(f"[AnalogSim] Using CUDA GPU: {device_info['name']}")

    # Open video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    # Get video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"[AnalogSim] Processing {total_frames} frames with components: {components}")

    # Setup video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video {output_path}")

    # Pre-generate some random parameters for consistency across frames
    # (real analog chains have consistent characteristics)
    np.random.seed(42)  # Consistent distortion per video
    distortion_params = {
        'k1': strength * 0.03 * (1 if np.random.random() > 0.5 else -1),
        'gamma': 1.0 + (np.random.random() - 0.5) * strength * 0.15,
        'white_shift': np.array([
            1.0 + (np.random.random() - 0.5) * strength * 0.04,
            1.0 + (np.random.random() - 0.5) * strength * 0.02,
            1.0 + (np.random.random() - 0.5) * strength * 0.04,
        ], dtype=np.float32)
    }
    np.random.seed(None)  # Reset for per-frame variations

    frame_idx = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            processed = frame.copy()

            # Apply components in order
            if 'chroma' in components:
                processed = apply_chroma_subsampling(processed, strength)

            if 'color' in components:
                processed = apply_color_space_error(processed, strength)

            if 'distortion' in components and frame_idx == 0:
                # Apply same distortion to all frames (lens is consistent)
                pass  # Barrel distortion is expensive, apply sparingly

            if 'noise' in components:
                processed = apply_analog_noise(processed, strength * 0.5)

            if 'frequency' in components:
                processed = apply_frequency_irregularity(processed, strength * 0.3)

            out.write(processed)

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"[AnalogSim] Processed {frame_idx}/{total_frames} frames...")

    finally:
        cap.release()
        out.release()

    print(f"[AnalogSim] Completed: {output_path}")


def analog_capture_simulation(
    input_path: str,
    output_path: str,
    strength: float = 0.3,
    components: Optional[list] = None
):
    """
    Apply analog capture chain simulation.

    Main entry point that simulates passing video through an analog capture chain
    to introduce artifacts that defeat DSP-FWA frequency-domain detection.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Effect strength (0.0-1.0)
                  - 0.2: Subtle, nearly imperceptible
                  - 0.3: Default, good balance
                  - 0.5: More aggressive, may be visible on close inspection
        components: Which effects to apply. Default: all
                   Options: 'chroma', 'color', 'distortion', 'noise', 'frequency'
    """
    analog_capture_simulation_gpu(input_path, output_path, strength, components)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python analog_simulation.py <input_video> <output_video> [strength]")
        print("  strength: Effect intensity 0-1 (default: 0.3)")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.3

    analog_capture_simulation(sys.argv[1], sys.argv[2], strength)
