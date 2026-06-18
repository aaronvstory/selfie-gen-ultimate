"""
Motion Energy Modulation for defeating AltFreezing smoothing detection.

This module adds realistic human micro-movements to video to defeat detectors
that identify synthetic video by its "over-stabilised" or "too smooth" motion:
- Micro-saccades and eye drift
- Breathing-induced motion
- Natural fidgeting patterns
- Variable motion intensity (activity envelope)

Face reenactment and puppeteering systems produce unnaturally smooth motion
because they operate in compressed latent spaces that lose high-frequency
temporal details. This module re-introduces natural motion characteristics.

Target Detectors: AltFreezing (motion smoothness detection)
Research Basis: Real humans exhibit constant micro-movements absent in synthetic video
"""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from typing import Optional, Tuple, List, Dict
from dataclasses import dataclass
from scipy import ndimage

# Try to import GPU utilities
try:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gpu_utils import get_device_info
    GPU_UTILS_AVAILABLE = True
except ImportError:
    GPU_UTILS_AVAILABLE = False


@dataclass
class MotionConfig:
    """Configuration for motion modulation."""
    # Micro-saccade parameters
    saccade_rate: float = 1.5          # Saccades per second
    saccade_amplitude_min: float = 0.3  # Min amplitude in pixels
    saccade_amplitude_max: float = 1.5  # Max amplitude in pixels
    drift_sigma: float = 0.02          # Drift random walk sigma
    tremor_sigma: float = 0.08         # High-frequency tremor sigma

    # Breathing parameters
    breath_rate_min: float = 0.2       # Min breath rate Hz (12 BPM)
    breath_rate_max: float = 0.33      # Max breath rate Hz (20 BPM)
    breath_amplitude_min: float = 1.0   # Min breathing motion pixels
    breath_amplitude_max: float = 3.0   # Max breathing motion pixels

    # Fidget parameters
    fidget_interval_mean: float = 5.0   # Mean seconds between fidgets
    fidget_duration_min: float = 0.3    # Min fidget duration seconds
    fidget_duration_max: float = 1.5    # Max fidget duration seconds
    fidget_magnitude_min: float = 0.5   # Min fidget magnitude pixels
    fidget_magnitude_max: float = 3.0   # Max fidget magnitude pixels

    # Activity envelope
    min_activity: float = 0.2          # Minimum activity level (stillness)
    max_activity: float = 1.0          # Maximum activity level
    activity_smoothing_seconds: float = 2.0  # Activity state transition time

    # Additional noise
    spatial_noise_sigma: float = 0.3   # Sub-pixel spatial jitter
    temporal_noise_sigma: float = 0.2  # Temporal noise on motion


class MotionEnergyModulator:
    """
    Adds realistic micro-movements to video to defeat smoothing detection.

    Combines multiple sources of natural human motion:
    - Eye micro-saccades (sporadic rapid movements)
    - Drift (slow random walk)
    - Tremor (high-frequency jitter)
    - Breathing (periodic head motion)
    - Fidgeting (sporadic larger movements)
    """

    def __init__(self, config: Optional[MotionConfig] = None, fps: int = 30):
        """
        Initialize motion modulator.

        Args:
            config: Motion configuration (uses defaults if None)
            fps: Video frame rate
        """
        self.config = config or MotionConfig()
        self.fps = fps

    def generate_composite_motion(self, num_frames: int) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate composite motion signal from all natural motion sources.

        Args:
            num_frames: Number of frames to generate motion for

        Returns:
            Tuple of (motion_x, motion_y) arrays
        """
        cfg = self.config

        # 1. Micro-saccades and eye tremor
        sac_x, sac_y = self._generate_microsaccades(num_frames)

        # 2. Breathing motion
        breath_x, breath_y = self._generate_breathing(num_frames)

        # 3. Fidgeting events
        fidgets = self._generate_fidget_events(num_frames)
        fidget_x, fidget_y = self._apply_fidgets(np.zeros(num_frames), np.zeros(num_frames), fidgets)

        # 4. Activity envelope
        activity = self._generate_activity_envelope(num_frames)

        # Combine with activity modulation
        motion_x = activity * (sac_x + breath_x + fidget_x)
        motion_y = activity * (sac_y + breath_y + fidget_y)

        # 5. Add temporal noise
        motion_x += np.random.normal(0, cfg.temporal_noise_sigma, num_frames)
        motion_y += np.random.normal(0, cfg.temporal_noise_sigma, num_frames)

        return motion_x.astype(np.float32), motion_y.astype(np.float32)

    def _generate_microsaccades(self, num_frames: int) -> Tuple[np.ndarray, np.ndarray]:
        """Generate eye micro-movement patterns."""
        cfg = self.config

        # Drift (random walk)
        drift_x = np.cumsum(np.random.normal(0, cfg.drift_sigma, num_frames))
        drift_y = np.cumsum(np.random.normal(0, cfg.drift_sigma, num_frames))

        # Mean-revert drift to prevent wandering
        drift_x = drift_x - ndimage.uniform_filter1d(drift_x, size=self.fps * 5)
        drift_y = drift_y - ndimage.uniform_filter1d(drift_y, size=self.fps * 5)

        # Micro-saccades (sporadic jumps)
        num_saccades = int(num_frames / self.fps * cfg.saccade_rate)
        if num_saccades > 0:
            saccade_times = np.random.choice(num_frames, size=min(num_saccades, num_frames), replace=False)
        else:
            saccade_times = []

        saccade_x = np.zeros(num_frames)
        saccade_y = np.zeros(num_frames)

        for st in saccade_times:
            amp = np.random.uniform(cfg.saccade_amplitude_min, cfg.saccade_amplitude_max)
            angle = np.random.uniform(0, 2 * np.pi)
            saccade_x[st] = amp * np.cos(angle)
            saccade_y[st] = amp * np.sin(angle)

        # Smooth saccades slightly (not instant)
        saccade_x = ndimage.gaussian_filter1d(saccade_x, sigma=1)
        saccade_y = ndimage.gaussian_filter1d(saccade_y, sigma=1)

        # High-frequency tremor
        tremor_x = np.random.normal(0, cfg.tremor_sigma, num_frames)
        tremor_y = np.random.normal(0, cfg.tremor_sigma, num_frames)

        return drift_x + saccade_x + tremor_x, drift_y + saccade_y + tremor_y

    def _generate_breathing(self, num_frames: int) -> Tuple[np.ndarray, np.ndarray]:
        """Generate breathing-induced head motion."""
        cfg = self.config
        t = np.arange(num_frames) / self.fps

        # Variable breathing rate
        base_freq = np.random.uniform(cfg.breath_rate_min, cfg.breath_rate_max)
        freq_var = 0.02 * np.sin(2 * np.pi * 0.01 * t)

        # Cumulative phase
        phase = 2 * np.pi * np.cumsum(base_freq + freq_var) / self.fps

        # Asymmetric waveform (longer exhale)
        inhale_ratio = 0.4
        waveform = np.zeros(num_frames)

        for i in range(num_frames):
            p = np.mod(phase[i], 2 * np.pi)
            if p < inhale_ratio * 2 * np.pi:
                waveform[i] = np.sin(p / inhale_ratio * 0.5)
            else:
                waveform[i] = np.cos((p - inhale_ratio * 2 * np.pi) / (1 - inhale_ratio) * 0.5)

        # Variable amplitude
        base_amp = np.random.uniform(cfg.breath_amplitude_min, cfg.breath_amplitude_max)
        amp_var = 0.3 * base_amp * np.sin(2 * np.pi * 0.05 * t)
        amplitude = base_amp + amp_var

        vertical = amplitude * waveform
        horizontal = 0.25 * vertical  # Less horizontal movement

        return horizontal, vertical

    def _generate_fidget_events(self, num_frames: int) -> List[Dict]:
        """Generate sporadic fidgeting events."""
        cfg = self.config
        fidgets = []
        t = 0

        while t < num_frames:
            interval = int(np.random.exponential(scale=self.fps * cfg.fidget_interval_mean))
            t += max(interval, self.fps)  # Minimum 1 second between fidgets

            if t < num_frames:
                duration = int(np.random.uniform(
                    cfg.fidget_duration_min * self.fps,
                    cfg.fidget_duration_max * self.fps
                ))
                fidgets.append({
                    'start': t,
                    'duration': duration,
                    'magnitude': np.random.uniform(cfg.fidget_magnitude_min, cfg.fidget_magnitude_max),
                    'direction': np.random.uniform(0, 2 * np.pi)
                })

        return fidgets

    def _apply_fidgets(
        self,
        motion_x: np.ndarray,
        motion_y: np.ndarray,
        fidgets: List[Dict]
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Apply fidget events to motion signal."""
        for f in fidgets:
            if f['duration'] < 2:
                continue

            # Smooth onset/offset using Hanning window
            window = np.hanning(f['duration'])
            dx = f['magnitude'] * np.cos(f['direction']) * window
            dy = f['magnitude'] * np.sin(f['direction']) * window

            end = min(f['start'] + f['duration'], len(motion_x))
            actual_len = end - f['start']

            motion_x[f['start']:end] += dx[:actual_len]
            motion_y[f['start']:end] += dy[:actual_len]

        return motion_x, motion_y

    def _generate_activity_envelope(self, num_frames: int) -> np.ndarray:
        """Generate time-varying activity level."""
        cfg = self.config

        # Base noise
        noise = np.random.randn(num_frames)

        # Smooth to create activity states
        kernel_size = int(cfg.activity_smoothing_seconds * self.fps)

        # Handle case where kernel is larger than signal (e.g., short chunks)
        if kernel_size > num_frames:
            kernel_size = num_frames if num_frames % 2 == 1 else num_frames - 1

        if kernel_size > 1:
            kernel = np.hanning(kernel_size)
            kernel /= kernel.sum()
            smoothed = np.convolve(noise, kernel, mode='same')
        else:
            smoothed = noise

        # Ensure output matches num_frames (safety check for edge cases)
        if len(smoothed) != num_frames:
            # Pad or trim to exact length
            if len(smoothed) < num_frames:
                smoothed = np.pad(smoothed, (0, num_frames - len(smoothed)), mode='edge')
            else:
                smoothed = smoothed[:num_frames]

        # Sigmoid transform for distinct high/low states
        activity = cfg.min_activity + (cfg.max_activity - cfg.min_activity) * \
                   (1 / (1 + np.exp(-2 * smoothed)))

        return activity.astype(np.float32)

    def apply_motion_to_frame(
        self,
        frame: np.ndarray,
        dx: float,
        dy: float
    ) -> np.ndarray:
        """
        Apply motion offset to a frame using sub-pixel interpolation.

        Args:
            frame: Input frame (H, W, C)
            dx, dy: Motion offsets in pixels

        Returns:
            Shifted frame
        """
        h, w = frame.shape[:2]

        # Create affine transform for translation
        M = np.float32([[1, 0, dx], [0, 1, dy]])

        return cv2.warpAffine(
            frame, M, (w, h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT
        )


def motion_energy_modulation_gpu(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    config: Optional[MotionConfig] = None
):
    """
    Apply motion energy modulation to defeat smoothing detection.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Strength multiplier (0.5 = subtle, 1.0 = normal, 2.0 = aggressive)
        config: Motion configuration (uses defaults if None)

    Raises:
        RuntimeError: If processing fails
    """
    if GPU_UTILS_AVAILABLE and torch.cuda.is_available():
        device_info = get_device_info()
        print(f"[MotionModulation] Using CUDA GPU: {device_info['name']}")

    # Open video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # CAP_PROP_FRAME_COUNT is unreliable, especially for freshly-written videos
    # For small videos (likely chunks), always count manually
    total_frames_meta = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # For short videos (<500 frames), always verify by counting manually
    # This is important for chunked processing where metadata may be wrong
    if total_frames_meta < 500:
        # Close and reopen to count frames reliably
        cap.release()
        cap = cv2.VideoCapture(input_path)

        total_frames = 0
        while True:
            ret, _ = cap.read()
            if not ret:
                break
            total_frames += 1

        # Close and reopen again for actual processing
        cap.release()
        cap = cv2.VideoCapture(input_path)

        if total_frames != total_frames_meta:
            print(f"[MotionModulation] Metadata correction: {total_frames_meta} -> {total_frames} frames")
    else:
        # For longer videos, trust metadata for performance
        total_frames = total_frames_meta

    print(f"[MotionModulation] Processing {total_frames} frames, strength={strength}")

    # Create scaled config based on strength
    if config is None:
        config = MotionConfig()

    # Scale motion amplitudes by strength
    scaled_config = MotionConfig(
        saccade_rate=config.saccade_rate,
        saccade_amplitude_min=config.saccade_amplitude_min * strength,
        saccade_amplitude_max=config.saccade_amplitude_max * strength,
        drift_sigma=config.drift_sigma * strength,
        tremor_sigma=config.tremor_sigma * strength,
        breath_rate_min=config.breath_rate_min,
        breath_rate_max=config.breath_rate_max,
        breath_amplitude_min=config.breath_amplitude_min * strength,
        breath_amplitude_max=config.breath_amplitude_max * strength,
        fidget_interval_mean=config.fidget_interval_mean,
        fidget_duration_min=config.fidget_duration_min,
        fidget_duration_max=config.fidget_duration_max,
        fidget_magnitude_min=config.fidget_magnitude_min * strength,
        fidget_magnitude_max=config.fidget_magnitude_max * strength,
        min_activity=config.min_activity,
        max_activity=config.max_activity,
        activity_smoothing_seconds=config.activity_smoothing_seconds,
        spatial_noise_sigma=config.spatial_noise_sigma * strength,
        temporal_noise_sigma=config.temporal_noise_sigma * strength,
    )

    # Initialize modulator
    modulator = MotionEnergyModulator(scaled_config, fps)

    # Pre-generate motion for entire video
    motion_x, motion_y = modulator.generate_composite_motion(total_frames)

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

            if frame_idx < len(motion_x):
                dx, dy = motion_x[frame_idx], motion_y[frame_idx]
            else:
                dx, dy = 0.0, 0.0

            # Apply motion
            modified = modulator.apply_motion_to_frame(frame, dx, dy)

            out.write(modified)

            frame_idx += 1
            if frame_idx % 100 == 0:
                print(f"[MotionModulation] Processed {frame_idx}/{total_frames} frames...")

    finally:
        cap.release()
        out.release()

    print(f"[MotionModulation] Completed: {output_path}")
    print(f"[MotionModulation] Motion stats - X: mean={np.mean(motion_x):.3f}, std={np.std(motion_x):.3f}")
    print(f"[MotionModulation] Motion stats - Y: mean={np.mean(motion_y):.3f}, std={np.std(motion_y):.3f}")


def motion_energy_modulation(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    config: Optional[MotionConfig] = None
):
    """
    Apply motion energy modulation to add natural human micro-movements.

    Main entry point for motion modulation that adds realistic motion patterns
    to defeat AltFreezing-style smoothing detection.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Effect strength (0.3-1.0 recommended)
                  - 0.3: Subtle micro-movements
                  - 0.5: Default, natural motion
                  - 1.0: More pronounced, may be visible
        config: Custom motion configuration (uses defaults if None)
    """
    motion_energy_modulation_gpu(input_path, output_path, strength, config)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python motion_modulation.py <input_video> <output_video> [strength]")
        print("  strength: Effect intensity 0-1 (default: 0.5)")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5

    motion_energy_modulation(sys.argv[1], sys.argv[2], strength)
