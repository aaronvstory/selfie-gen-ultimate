"""
Micro-Jitter Injection Module for defeating FTCN temporal similarity detection.

This module adds random sub-pixel motion per frame to break temporal consistency
patterns that replay/pre-recorded video detectors rely on. Real camera footage
has natural micro-jitter from sensor vibration, rolling shutter, and hand shake
that synthetic/replayed video lacks.

Target Detectors: DSP-FWA, FTCN
Research Basis: Temporal similarity checks fail when random sub-pixel motion is present
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


def generate_jitter_sequence(
    n_frames: int,
    max_amplitude: float = 0.5,
    temporal_correlation: float = 0.7,
    seed: Optional[int] = None
) -> np.ndarray:
    """
    Generate a temporally correlated sequence of jitter offsets.

    Real camera shake has temporal correlation - it doesn't jump randomly
    between frames but follows a smooth random walk pattern.

    Args:
        n_frames: Number of frames
        max_amplitude: Maximum jitter amplitude in pixels (0.3-1.0 typical)
        temporal_correlation: Correlation between consecutive frames (0.6-0.9 typical)
        seed: Random seed for reproducibility

    Returns:
        Array of shape (n_frames, 2) with (dx, dy) offsets for each frame
    """
    if seed is not None:
        np.random.seed(seed)

    # Generate correlated random walk for natural motion
    jitter = np.zeros((n_frames, 2), dtype=np.float32)

    # Initial random offset
    jitter[0] = np.random.randn(2) * max_amplitude * 0.5

    for i in range(1, n_frames):
        # Correlated random walk with mean reversion
        innovation = np.random.randn(2) * max_amplitude * (1 - temporal_correlation)
        jitter[i] = temporal_correlation * jitter[i-1] + innovation

        # Add occasional larger movements (simulating natural hand tremor)
        if np.random.random() < 0.05:  # 5% chance of larger movement
            jitter[i] += np.random.randn(2) * max_amplitude * 0.3

        # Soft clamp to prevent drift too far from center
        jitter[i] = np.tanh(jitter[i] / max_amplitude) * max_amplitude

    return jitter


def apply_subpixel_shift_gpu(
    frame_tensor: torch.Tensor,
    dx: float,
    dy: float
) -> torch.Tensor:
    """
    Apply sub-pixel shift to a frame using GPU-accelerated bilinear interpolation.

    Args:
        frame_tensor: Input frame tensor (1, C, H, W) on GPU
        dx: Horizontal shift in pixels (can be fractional)
        dy: Vertical shift in pixels (can be fractional)

    Returns:
        Shifted frame tensor
    """
    device = frame_tensor.device
    _, _, H, W = frame_tensor.shape

    # Create sampling grid with offset
    # grid_sample expects coordinates in [-1, 1] range
    y_coords = torch.linspace(-1, 1, H, device=device)
    x_coords = torch.linspace(-1, 1, W, device=device)

    # Convert pixel offset to normalized coordinates
    dx_norm = 2.0 * dx / W
    dy_norm = 2.0 * dy / H

    # Create mesh grid and apply offset
    grid_y, grid_x = torch.meshgrid(y_coords, x_coords, indexing='ij')
    grid_x = grid_x - dx_norm  # Subtract to shift content in positive direction
    grid_y = grid_y - dy_norm

    # Stack into sampling grid (1, H, W, 2)
    grid = torch.stack([grid_x, grid_y], dim=-1).unsqueeze(0)

    # Apply grid sample with bilinear interpolation
    shifted = F.grid_sample(
        frame_tensor,
        grid,
        mode='bilinear',
        padding_mode='reflection',  # Reflect at edges to avoid black borders
        align_corners=True
    )

    return shifted


def apply_subpixel_shift_cpu(
    frame: np.ndarray,
    dx: float,
    dy: float
) -> np.ndarray:
    """
    Apply sub-pixel shift to a frame using CPU with OpenCV.

    Args:
        frame: Input frame (H, W, C) as numpy array
        dx: Horizontal shift in pixels
        dy: Vertical shift in pixels

    Returns:
        Shifted frame as numpy array
    """
    H, W = frame.shape[:2]

    # Create affine transformation matrix for translation
    M = np.float32([
        [1, 0, dx],
        [0, 1, dy]
    ])

    # Apply warp with bilinear interpolation and border reflection
    shifted = cv2.warpAffine(
        frame,
        M,
        (W, H),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT
    )

    return shifted


def micro_jitter_injection_gpu(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    temporal_correlation: float = 0.7,
    seed: Optional[int] = None
):
    """
    Apply GPU-accelerated micro-jitter injection to defeat FTCN temporal detectors.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Jitter amplitude in pixels (0.3-1.0 recommended, higher = more visible)
        temporal_correlation: Temporal smoothness of jitter (0.6-0.9 recommended)
        seed: Random seed for reproducibility

    Raises:
        RuntimeError: If CUDA is not available or processing fails
    """
    if GPU_UTILS_AVAILABLE:
        device_info = get_device_info()
        print(f"[MicroJitter] Using CUDA GPU: {device_info['name']}")

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available for GPU acceleration")

    device = torch.device('cuda')

    # Open video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    # Get video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # CAP_PROP_FRAME_COUNT is unreliable for short/chunk videos
    total_frames_meta = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # For short videos (<500 frames), count manually
    if total_frames_meta < 500:
        cap.release()
        cap = cv2.VideoCapture(input_path)

        total_frames = 0
        while True:
            ret, _ = cap.read()
            if not ret:
                break
            total_frames += 1

        cap.release()
        cap = cv2.VideoCapture(input_path)

        if total_frames != total_frames_meta:
            print(f"[MicroJitter] Metadata correction: {total_frames_meta} -> {total_frames} frames")
    else:
        total_frames = total_frames_meta

    print(f"[MicroJitter] Processing {total_frames} frames, amplitude={strength}px, correlation={temporal_correlation}")

    # Generate jitter sequence for all frames
    jitter_sequence = generate_jitter_sequence(
        total_frames,
        max_amplitude=strength,
        temporal_correlation=temporal_correlation,
        seed=seed
    )

    # Setup video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video {output_path}")

    frame_idx = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Get jitter offset for this frame
            dx, dy = jitter_sequence[frame_idx]

            # Convert to tensor and move to GPU
            frame_tensor = torch.from_numpy(frame).float().permute(2, 0, 1).unsqueeze(0).to(device)

            # Apply sub-pixel shift
            shifted_tensor = apply_subpixel_shift_gpu(frame_tensor, dx, dy)

            # Convert back to numpy
            shifted_frame = shifted_tensor.squeeze(0).permute(1, 2, 0).cpu().numpy().astype(np.uint8)

            out.write(shifted_frame)

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"[MicroJitter] Processed {frame_idx}/{total_frames} frames...")

    finally:
        cap.release()
        out.release()

    print(f"[MicroJitter] Completed: {output_path}")


def micro_jitter_injection_cpu(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    temporal_correlation: float = 0.7,
    seed: Optional[int] = None
):
    """
    Apply CPU-based micro-jitter injection (fallback when GPU unavailable).

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Jitter amplitude in pixels
        temporal_correlation: Temporal smoothness of jitter
        seed: Random seed for reproducibility
    """
    print("[MicroJitter] Using CPU fallback")

    # Open video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    # Get video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # CAP_PROP_FRAME_COUNT is unreliable for short/chunk videos
    total_frames_meta = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # For short videos (<500 frames), count manually
    if total_frames_meta < 500:
        cap.release()
        cap = cv2.VideoCapture(input_path)

        total_frames = 0
        while True:
            ret, _ = cap.read()
            if not ret:
                break
            total_frames += 1

        cap.release()
        cap = cv2.VideoCapture(input_path)

        if total_frames != total_frames_meta:
            print(f"[MicroJitter] Metadata correction: {total_frames_meta} -> {total_frames} frames")
    else:
        total_frames = total_frames_meta

    print(f"[MicroJitter] Processing {total_frames} frames, amplitude={strength}px")

    # Generate jitter sequence
    jitter_sequence = generate_jitter_sequence(
        total_frames,
        max_amplitude=strength,
        temporal_correlation=temporal_correlation,
        seed=seed
    )

    # Setup video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video {output_path}")

    frame_idx = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            dx, dy = jitter_sequence[frame_idx]
            shifted_frame = apply_subpixel_shift_cpu(frame, dx, dy)
            out.write(shifted_frame)

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"[MicroJitter] Processed {frame_idx}/{total_frames} frames...")

    finally:
        cap.release()
        out.release()

    print(f"[MicroJitter] Completed: {output_path}")


def micro_jitter_injection(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    temporal_correlation: float = 0.7,
    seed: Optional[int] = None
):
    """
    Apply micro-jitter injection with automatic GPU/CPU selection.

    This is the main entry point that selects the best available backend.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Jitter amplitude in pixels (0.3-1.0 recommended)
                  - 0.3: Very subtle, nearly imperceptible
                  - 0.5: Default, good balance
                  - 1.0: More aggressive, may be slightly visible
        temporal_correlation: How smooth the jitter is between frames (0.6-0.9)
                              - 0.6: More random, less natural
                              - 0.7: Default, natural camera shake
                              - 0.9: Very smooth, subtle drift
        seed: Random seed for reproducible results
    """
    if torch.cuda.is_available():
        micro_jitter_injection_gpu(input_path, output_path, strength, temporal_correlation, seed)
    else:
        micro_jitter_injection_cpu(input_path, output_path, strength, temporal_correlation, seed)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python micro_jitter.py <input_video> <output_video> [strength] [correlation]")
        print("  strength: Jitter amplitude in pixels (default: 0.5)")
        print("  correlation: Temporal smoothness 0-1 (default: 0.7)")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    correlation = float(sys.argv[4]) if len(sys.argv) > 4 else 0.7

    micro_jitter_injection(sys.argv[1], sys.argv[2], strength, correlation)
