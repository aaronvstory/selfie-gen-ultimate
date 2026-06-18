"""
Frame-Rate Chaos / Variable FPS for defeating temporal analysis detectors.

This module introduces realistic camera-like timing irregularities to defeat
detectors that rely on frame timing consistency as a signal:
- PTS jitter (temporally correlated Gaussian noise)
- Frame dropping (simulates capture failures)
- Frame duplication (simulates buffer repeats)
- Rolling shutter simulation

Real cameras have inherent timing jitter from USB latency, sensor readout,
auto-exposure adjustments, and software pipeline delays. Synthetic video
has essentially perfect timing regularity which detectors can identify.

Target Detectors: FTCN, AltFreezing, DSP-FWA (temporal components)
Research Basis: Synthetic video has ~0ms timing std dev, real webcams have 2-15ms
"""

import os
import cv2
import numpy as np
import subprocess
import tempfile
import shutil
from typing import Optional, List, Tuple, Dict, Any
from dataclasses import dataclass
from enum import Enum
import random

# Try to import GPU utilities
try:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gpu_utils import get_device_info
    GPU_UTILS_AVAILABLE = True
except ImportError:
    GPU_UTILS_AVAILABLE = False


class CameraProfile(Enum):
    """Predefined camera timing profiles."""
    HIGH_QUALITY_PHONE = "high_quality_phone"
    LAPTOP_WEBCAM = "laptop_webcam"
    CHEAP_USB_WEBCAM = "cheap_usb_webcam"
    BROWSER_WEBRTC = "browser_webrtc"


@dataclass
class TimingParams:
    """Parameters for frame timing jitter."""
    jitter_std_ms: float        # Standard deviation of PTS jitter
    correlation: float          # Temporal correlation (0=white, 1=fully correlated)
    drop_probability: float     # Probability of dropping a frame
    dup_probability: float      # Probability of duplicating a frame
    burst_probability: float    # Probability of burst drop/dup
    rolling_shutter_ms: float   # Rolling shutter duration

    @classmethod
    def from_profile(cls, profile: CameraProfile) -> 'TimingParams':
        """Create parameters from predefined profile."""
        profiles = {
            CameraProfile.HIGH_QUALITY_PHONE: cls(
                jitter_std_ms=2.0, correlation=0.6,
                drop_probability=0.005, dup_probability=0.005,
                burst_probability=0.001, rolling_shutter_ms=12.0
            ),
            CameraProfile.LAPTOP_WEBCAM: cls(
                jitter_std_ms=4.0, correlation=0.5,
                drop_probability=0.015, dup_probability=0.01,
                burst_probability=0.003, rolling_shutter_ms=22.0
            ),
            CameraProfile.CHEAP_USB_WEBCAM: cls(
                jitter_std_ms=6.0, correlation=0.4,
                drop_probability=0.025, dup_probability=0.02,
                burst_probability=0.005, rolling_shutter_ms=28.0
            ),
            CameraProfile.BROWSER_WEBRTC: cls(
                jitter_std_ms=12.0, correlation=0.3,
                drop_probability=0.03, dup_probability=0.025,
                burst_probability=0.008, rolling_shutter_ms=25.0
            ),
        }
        return profiles.get(profile, profiles[CameraProfile.LAPTOP_WEBCAM])


class FrameRateChaos:
    """
    Main class for applying frame rate chaos to video.

    Combines:
    - PTS jitter (temporally correlated)
    - Frame dropping
    - Frame duplication
    - Rolling shutter simulation
    """

    def __init__(self, params: Optional[TimingParams] = None):
        """
        Initialize with timing parameters.

        Args:
            params: Timing parameters (uses laptop webcam profile if None)
        """
        self.params = params or TimingParams.from_profile(CameraProfile.LAPTOP_WEBCAM)
        self.prev_jitter = 0.0
        self.stats: Dict[str, Any] = {}

    def _generate_jitter(self) -> float:
        """Generate temporally correlated jitter."""
        new_jitter = np.random.normal(0, self.params.jitter_std_ms)
        jitter = (self.params.correlation * self.prev_jitter +
                  (1 - self.params.correlation) * new_jitter)
        self.prev_jitter = jitter
        return jitter

    def _decide_action(self, frame_idx: int) -> str:
        """Decide frame action: keep, drop, or duplicate."""
        r = random.random()

        if r < self.params.drop_probability:
            return 'drop'
        elif r < self.params.drop_probability + self.params.dup_probability:
            return 'duplicate'
        return 'keep'

    def _get_burst_count(self) -> int:
        """Get number of frames for burst duplication."""
        if random.random() < self.params.burst_probability:
            return random.randint(2, 4)
        return 1

    def _apply_rolling_shutter(
        self,
        prev_frame: np.ndarray,
        curr_frame: np.ndarray,
        fps: float
    ) -> np.ndarray:
        """Apply simplified rolling shutter effect."""
        height = curr_frame.shape[0]
        frame_duration_ms = 1000.0 / fps
        rs_ratio = self.params.rolling_shutter_ms / frame_duration_ms
        blend_rows = int(height * min(rs_ratio, 0.5))

        if blend_rows <= 0:
            return curr_frame

        result = curr_frame.copy().astype(np.float32)
        prev_float = prev_frame.astype(np.float32)

        for row in range(blend_rows):
            alpha = row / blend_rows
            result[row] = alpha * curr_frame[row] + (1 - alpha) * prev_float[row]

        return result.astype(np.uint8)

    def process(
        self,
        input_path: str,
        output_path: str,
        apply_rolling_shutter: bool = True
    ) -> Dict[str, Any]:
        """
        Apply frame rate chaos to video.

        Args:
            input_path: Input video path
            output_path: Output video path
            apply_rolling_shutter: Whether to simulate rolling shutter

        Returns:
            Statistics dictionary
        """
        # Open video
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open video {input_path}")

        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        # CAP_PROP_FRAME_COUNT is unreliable for short/chunk videos
        total_frames_meta = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        # For short videos (<500 frames), count manually to ensure accuracy
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
                print(f"[FrameChaos] Metadata correction: {total_frames_meta} -> {total_frames} frames")
        else:
            total_frames = total_frames_meta

        frame_duration_ms = 1000.0 / fps

        self.stats = {
            'input_frames': total_frames,
            'input_fps': fps,
            'dropped': 0,
            'duplicated': 0,
            'output_frames': 0,
            'avg_jitter_ms': 0.0
        }

        # Collect frames with timing info
        frames_data: List[Tuple[np.ndarray, float]] = []
        current_pts = 0.0
        self.prev_jitter = 0.0
        jitter_sum = 0.0
        prev_frame = None

        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Apply rolling shutter
            if apply_rolling_shutter and prev_frame is not None:
                frame = self._apply_rolling_shutter(prev_frame, frame, fps)

            # Decide frame action
            action = self._decide_action(frame_idx)

            if action == 'drop':
                self.stats['dropped'] += 1
                current_pts += frame_duration_ms
                frame_idx += 1
                prev_frame = frame
                continue

            # Add frame with jittered PTS
            jitter = self._generate_jitter()
            jitter_sum += abs(jitter)
            pts = current_pts + jitter
            frames_data.append((frame.copy(), pts))
            self.stats['output_frames'] += 1

            # Handle duplication
            if action == 'duplicate':
                dup_count = self._get_burst_count()
                for _ in range(dup_count):
                    current_pts += frame_duration_ms
                    jitter = self._generate_jitter()
                    jitter_sum += abs(jitter)
                    frames_data.append((frame.copy(), current_pts + jitter))
                    self.stats['duplicated'] += 1
                    self.stats['output_frames'] += 1

            current_pts += frame_duration_ms
            frame_idx += 1
            prev_frame = frame

            if frame_idx % 100 == 0:
                print(f"[FrameChaos] Processing frame {frame_idx}/{total_frames}...")

        cap.release()

        if frames_data:
            self.stats['avg_jitter_ms'] = jitter_sum / len(frames_data)

        # Sort by PTS
        frames_data.sort(key=lambda x: x[1])

        # Write output
        self._write_video(frames_data, output_path, fps, width, height, frame_duration_ms)

        return self.stats

    def _write_video(
        self,
        frames_data: List[Tuple[np.ndarray, float]],
        output_path: str,
        fps: float,
        width: int,
        height: int,
        frame_duration_ms: float
    ):
        """Write frames to video file."""
        # Check if ffmpeg is available for VFR output
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
            ffmpeg_available = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            ffmpeg_available = False

        if ffmpeg_available:
            self._write_vfr_ffmpeg(frames_data, output_path, fps, frame_duration_ms)
        else:
            # Fallback to OpenCV (loses VFR timing info but applies visual effects)
            self._write_cv2(frames_data, output_path, fps, width, height)

    def _write_vfr_ffmpeg(
        self,
        frames_data: List[Tuple[np.ndarray, float]],
        output_path: str,
        fps: float,
        frame_duration_ms: float
    ):
        """Write video with variable frame rate using ffmpeg."""
        temp_dir = tempfile.mkdtemp()
        concat_file = os.path.join(temp_dir, 'concat.txt')

        try:
            with open(concat_file, 'w') as f:
                for i, (frame, pts) in enumerate(frames_data):
                    frame_path = os.path.join(temp_dir, f'frame_{i:06d}.png')
                    cv2.imwrite(frame_path, frame)

                    # Calculate duration to next frame
                    if i < len(frames_data) - 1:
                        duration = (frames_data[i + 1][1] - pts) / 1000.0
                    else:
                        duration = frame_duration_ms / 1000.0

                    duration = max(0.001, min(duration, 1.0))

                    f.write(f"file '{frame_path}'\n")
                    f.write(f"duration {duration:.6f}\n")

            cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-vsync', 'vfr',
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-crf', '18',
                '-pix_fmt', 'yuv420p',
                output_path
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"[FrameChaos] FFmpeg warning: {result.stderr[:200]}")
                # Fall back to OpenCV
                self._write_cv2_from_temp(temp_dir, output_path, fps, frames_data)

        finally:
            shutil.rmtree(temp_dir)

    def _write_cv2_from_temp(
        self,
        temp_dir: str,
        output_path: str,
        fps: float,
        frames_data: List[Tuple[np.ndarray, float]]
    ):
        """Write video using OpenCV from temp frames."""
        if not frames_data:
            return

        height, width = frames_data[0][0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

        for frame, _ in frames_data:
            out.write(frame)

        out.release()

    def _write_cv2(
        self,
        frames_data: List[Tuple[np.ndarray, float]],
        output_path: str,
        fps: float,
        width: int,
        height: int
    ):
        """Write video using OpenCV (CFR fallback)."""
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

        for frame, _ in frames_data:
            out.write(frame)

        out.release()


def frame_rate_chaos_attack(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    profile: str = "laptop_webcam",
    apply_rolling_shutter: bool = True
) -> Dict[str, Any]:
    """
    Apply frame rate chaos attack to video.

    Args:
        input_path: Input video path
        output_path: Output video path
        strength: Strength multiplier (0.5 = subtle, 1.0 = normal, 2.0 = aggressive)
        profile: Camera profile to simulate
                 Options: 'high_quality_phone', 'laptop_webcam', 'cheap_webcam', 'browser'
        apply_rolling_shutter: Whether to apply rolling shutter simulation

    Returns:
        Processing statistics
    """
    profile_map = {
        "high_quality_phone": CameraProfile.HIGH_QUALITY_PHONE,
        "laptop_webcam": CameraProfile.LAPTOP_WEBCAM,
        "cheap_webcam": CameraProfile.CHEAP_USB_WEBCAM,
        "cheap_usb_webcam": CameraProfile.CHEAP_USB_WEBCAM,
        "browser": CameraProfile.BROWSER_WEBRTC,
        "browser_webrtc": CameraProfile.BROWSER_WEBRTC,
    }

    camera_profile = profile_map.get(profile, CameraProfile.LAPTOP_WEBCAM)
    params = TimingParams.from_profile(camera_profile)

    # Scale parameters by strength
    params.jitter_std_ms *= strength
    params.drop_probability *= strength
    params.dup_probability *= strength
    params.burst_probability *= strength

    print(f"[FrameChaos] Profile: {profile}, strength: {strength}")
    print(f"[FrameChaos] Jitter std: {params.jitter_std_ms:.1f}ms, "
          f"drop: {params.drop_probability*100:.1f}%, dup: {params.dup_probability*100:.1f}%")

    chaos = FrameRateChaos(params)
    return chaos.process(input_path, output_path, apply_rolling_shutter)


def frame_rate_chaos(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    profile: str = "laptop_webcam"
):
    """
    Apply frame rate chaos to introduce realistic camera timing irregularities.

    Main entry point for frame timing manipulation that defeats temporal analysis
    detectors looking for unnaturally perfect frame timing.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Effect strength (0.3-1.0 recommended)
                  - 0.3: Subtle, high-quality camera simulation
                  - 0.5: Default, typical webcam
                  - 1.0: Aggressive, browser/low-quality simulation
        profile: Camera profile to simulate
                 Options: 'high_quality_phone', 'laptop_webcam', 'cheap_webcam', 'browser'
    """
    stats = frame_rate_chaos_attack(input_path, output_path, strength, profile)

    print(f"\n[FrameChaos] Processing complete:")
    print(f"  Input frames: {stats['input_frames']}")
    print(f"  Output frames: {stats['output_frames']}")
    print(f"  Dropped: {stats['dropped']}")
    print(f"  Duplicated: {stats['duplicated']}")
    print(f"  Average jitter: {stats['avg_jitter_ms']:.2f}ms")


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python frame_chaos.py <input_video> <output_video> [strength] [profile]")
        print("  strength: Effect intensity 0-1 (default: 0.5)")
        print("  profile: high_quality_phone/laptop_webcam/cheap_webcam/browser (default: laptop_webcam)")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    profile = sys.argv[4] if len(sys.argv) > 4 else "laptop_webcam"

    frame_rate_chaos(sys.argv[1], sys.argv[2], strength, profile)
