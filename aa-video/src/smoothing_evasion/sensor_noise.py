"""
Realistic Camera Sensor Noise Simulation for defeating AltFreezing smoothing detection.

This module implements physically-based camera sensor noise that mimics real cameras:
- Photon/shot noise (Poisson distributed, signal-dependent)
- Read noise (Gaussian, signal-independent)
- Fixed Pattern Noise (PRNU/DSNU - consistent across frames)
- Temporal noise correlation following motion

Synthetic/AI-generated videos lack realistic sensor noise patterns, which
AltFreezing-style detectors can identify. This module adds authentic noise
characteristics to evade such detection.

Target Detectors: AltFreezing (smoothing detection), general authenticity checks
Research Basis: Real cameras have characteristic noise distributions that AI video lacks
"""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from typing import Optional, Tuple, Dict
from dataclasses import dataclass
from enum import Enum

# Try to import GPU utilities
try:
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
    from gpu_utils import get_device_info
    GPU_UTILS_AVAILABLE = True
except ImportError:
    GPU_UTILS_AVAILABLE = False


class SensorType(Enum):
    """Predefined sensor profiles for different camera types."""
    SMARTPHONE = "smartphone"
    APSC = "apsc"
    FULLFRAME = "fullframe"
    WEBCAM = "webcam"


@dataclass
class SensorParams:
    """Physical parameters for a camera sensor."""
    read_noise_e: float      # Read noise in electrons
    full_well: int           # Full well capacity in electrons
    quantum_efficiency: float # QE (0-1)
    dark_current: float      # e-/pixel/frame
    prnu_sigma: float        # Photo Response Non-Uniformity (multiplicative)
    dsnu_sigma: float        # Dark Signal Non-Uniformity (additive)
    pixel_pitch_um: float    # Pixel size in micrometers

    @classmethod
    def from_type(cls, sensor_type: SensorType) -> 'SensorParams':
        """Create parameters from predefined sensor type."""
        params = {
            SensorType.SMARTPHONE: cls(
                read_noise_e=3.5, full_well=4500, quantum_efficiency=0.5,
                dark_current=0.02, prnu_sigma=0.015, dsnu_sigma=0.005,
                pixel_pitch_um=1.0
            ),
            SensorType.APSC: cls(
                read_noise_e=2.5, full_well=30000, quantum_efficiency=0.6,
                dark_current=0.01, prnu_sigma=0.010, dsnu_sigma=0.003,
                pixel_pitch_um=4.0
            ),
            SensorType.FULLFRAME: cls(
                read_noise_e=2.0, full_well=60000, quantum_efficiency=0.65,
                dark_current=0.008, prnu_sigma=0.008, dsnu_sigma=0.002,
                pixel_pitch_um=6.0
            ),
            SensorType.WEBCAM: cls(
                read_noise_e=4.5, full_well=3500, quantum_efficiency=0.45,
                dark_current=0.03, prnu_sigma=0.020, dsnu_sigma=0.008,
                pixel_pitch_um=1.4
            ),
        }
        return params.get(sensor_type, params[SensorType.SMARTPHONE])


class RealisticSensorNoise:
    """
    Physically-based camera sensor noise simulation.

    Models:
    - Shot noise (Poisson, signal-dependent)
    - Read noise (Gaussian, constant)
    - Fixed Pattern Noise (PRNU multiplicative, DSNU additive)
    - Hot pixels (elevated dark current)
    """

    def __init__(
        self,
        iso: int = 400,
        sensor_type: SensorType = SensorType.SMARTPHONE,
        seed: Optional[int] = None
    ):
        """
        Initialize sensor noise generator.

        Args:
            iso: Simulated ISO level (100-6400+)
            sensor_type: Type of sensor to simulate
            seed: Random seed for reproducible FPN patterns
        """
        self.iso = iso
        self.params = SensorParams.from_type(sensor_type)
        self.sensor_type = sensor_type

        # Random state for FPN (consistent per "camera")
        self.fpn_seed = seed if seed is not None else np.random.randint(0, 2**31)
        self.rng = np.random.default_rng(self.fpn_seed)

        # Calculate gain from ISO (ISO 100 = 1x gain)
        self.analog_gain = iso / 100.0

        # FPN maps (initialized lazily)
        self._prnu_map: Optional[np.ndarray] = None
        self._dsnu_map: Optional[np.ndarray] = None
        self._hot_pixels: Optional[list] = None

    def _init_fpn_maps(self, shape: Tuple[int, int, int]):
        """Initialize Fixed Pattern Noise maps for this camera instance."""
        h, w, c = shape

        # Reset RNG for consistent FPN
        fpn_rng = np.random.default_rng(self.fpn_seed)

        # PRNU: multiplicative variation per pixel
        # Has slight spatial correlation (not pure white noise)
        self._prnu_map = np.ones((h, w, c), dtype=np.float32)
        for ch in range(c):
            base_prnu = fpn_rng.normal(0, self.params.prnu_sigma, (h, w))
            # Add spatial correlation via blur
            base_prnu = cv2.GaussianBlur(base_prnu.astype(np.float32), (5, 5), 1.0)
            self._prnu_map[:, :, ch] = 1.0 + base_prnu

        # DSNU: additive dark signal variation
        self._dsnu_map = fpn_rng.normal(0, self.params.dsnu_sigma, (h, w, c)).astype(np.float32)
        self._dsnu_map = cv2.GaussianBlur(self._dsnu_map, (3, 3), 0.5)

        # Hot pixels: random pixels with elevated dark current
        num_hot = int(0.0001 * h * w)  # 0.01% hot pixels
        hot_y = fpn_rng.integers(0, h, num_hot)
        hot_x = fpn_rng.integers(0, w, num_hot)
        hot_values = fpn_rng.uniform(0.1, 0.5, num_hot)
        self._hot_pixels = list(zip(hot_y, hot_x, hot_values))

    def _apply_shot_noise(self, linear_image: np.ndarray) -> np.ndarray:
        """Apply Poisson-distributed shot noise (signal-dependent)."""
        # Scale to electron counts
        electrons = linear_image * self.params.full_well * self.analog_gain

        # For large counts, use Gaussian approximation (faster)
        # For small counts, true Poisson
        noisy = np.zeros_like(electrons)

        mask_low = electrons < 50
        mask_high = ~mask_low

        # True Poisson for low counts
        if np.any(mask_low):
            noisy[mask_low] = np.random.poisson(np.maximum(electrons[mask_low], 0.1))

        # Gaussian approximation for high counts
        if np.any(mask_high):
            sigma = np.sqrt(np.maximum(electrons[mask_high], 1))
            noisy[mask_high] = electrons[mask_high] + np.random.randn(np.sum(mask_high)) * sigma

        # Convert back to normalized range
        return np.clip(noisy / (self.params.full_well * self.analog_gain), 0, 1)

    def _apply_read_noise(self, image: np.ndarray) -> np.ndarray:
        """Apply Gaussian read noise (signal-independent)."""
        read_noise_normalized = (self.params.read_noise_e * self.analog_gain) / self.params.full_well
        noise = np.random.normal(0, read_noise_normalized, image.shape).astype(np.float32)
        return image + noise

    def _apply_fpn(self, image: np.ndarray) -> np.ndarray:
        """Apply Fixed Pattern Noise (PRNU and DSNU)."""
        # PRNU: multiplicative
        image = image * self._prnu_map

        # DSNU: additive
        image = image + self._dsnu_map * self.analog_gain

        # Hot pixels
        for y, x, val in self._hot_pixels:
            image[y, x, :] += val * self.analog_gain

        return image

    def _apply_dark_current(self, image: np.ndarray) -> np.ndarray:
        """Apply dark current with shot noise."""
        dark = self.params.dark_current * self.analog_gain / self.params.full_well
        # Dark current also has shot noise
        dark_noise = np.sqrt(dark) * np.random.randn(*image.shape).astype(np.float32)
        return image + dark + dark_noise

    def add_noise(self, frame: np.ndarray) -> np.ndarray:
        """
        Add realistic camera sensor noise to a frame.

        Args:
            frame: Input frame (uint8, BGR, 0-255)

        Returns:
            Noised frame (uint8, BGR, 0-255)
        """
        # Initialize FPN maps if needed
        if self._prnu_map is None or self._prnu_map.shape != frame.shape:
            self._init_fpn_maps(frame.shape)

        # Convert to float [0, 1]
        image = frame.astype(np.float32) / 255.0

        # Undo gamma (approximate sRGB -> linear)
        linear = np.power(np.maximum(image, 1e-8), 2.2)

        # Apply noise in physically correct order:
        # 1. Fixed pattern noise (PRNU - multiplicative)
        # 2. Dark current
        # 3. Shot noise (Poisson)
        # 4. Read noise (Gaussian)
        noisy = self._apply_fpn(linear)
        noisy = self._apply_dark_current(noisy)
        noisy = self._apply_shot_noise(noisy)
        noisy = self._apply_read_noise(noisy)

        # Clip to valid range
        noisy = np.clip(noisy, 0, 1)

        # Apply gamma (linear -> sRGB)
        gamma_corrected = np.power(noisy, 1/2.2)

        return (gamma_corrected * 255).astype(np.uint8)


class RealisticSensorNoiseGPU:
    """GPU-accelerated realistic sensor noise using PyTorch."""

    def __init__(
        self,
        iso: int = 400,
        sensor_type: SensorType = SensorType.SMARTPHONE,
        device: str = 'cuda',
        seed: Optional[int] = None
    ):
        self.iso = iso
        self.params = SensorParams.from_type(sensor_type)
        self.device = torch.device(device if torch.cuda.is_available() else 'cpu')
        self.analog_gain = iso / 100.0

        self.fpn_seed = seed if seed is not None else np.random.randint(0, 2**31)
        if seed is not None:
            torch.manual_seed(seed)

        # FPN maps (lazy init)
        self._prnu_map: Optional[torch.Tensor] = None
        self._dsnu_map: Optional[torch.Tensor] = None

    def _init_fpn_maps_gpu(self, shape: Tuple[int, int, int, int]):
        """Initialize FPN maps on GPU. Shape: (B, C, H, W)"""
        _, c, h, w = shape

        # PRNU
        prnu_base = torch.randn(1, c, h, w, device=self.device) * self.params.prnu_sigma
        # Spatial correlation via average pooling
        prnu_smooth = F.avg_pool2d(prnu_base, kernel_size=5, stride=1, padding=2)
        self._prnu_map = 1.0 + prnu_smooth

        # DSNU
        dsnu_base = torch.randn(1, c, h, w, device=self.device) * self.params.dsnu_sigma
        self._dsnu_map = F.avg_pool2d(dsnu_base, kernel_size=3, stride=1, padding=1)

    def add_noise_batch(self, frames: torch.Tensor) -> torch.Tensor:
        """
        Add realistic noise to a batch of frames on GPU.

        Args:
            frames: Tensor of shape (B, C, H, W), float32, range [0, 1]

        Returns:
            Noised frames, same shape and range
        """
        if self._prnu_map is None or self._prnu_map.shape[2:] != frames.shape[2:]:
            self._init_fpn_maps_gpu(frames.shape)

        # Undo gamma
        linear = torch.pow(torch.clamp(frames, min=1e-8), 2.2)

        # Apply PRNU
        noisy = linear * self._prnu_map

        # Apply DSNU
        noisy = noisy + self._dsnu_map * self.analog_gain

        # Dark current with noise
        dark = self.params.dark_current * self.analog_gain / self.params.full_well
        dark_noise = torch.sqrt(torch.tensor(dark, device=self.device)) * torch.randn_like(noisy)
        noisy = noisy + dark + dark_noise

        # Shot noise (Gaussian approximation)
        electrons = noisy * self.params.full_well * self.analog_gain
        sigma = torch.sqrt(torch.clamp(electrons, min=1.0))
        shot_noise = torch.randn_like(electrons) * sigma
        noisy_electrons = electrons + shot_noise
        noisy = torch.clamp(noisy_electrons / (self.params.full_well * self.analog_gain), 0, 1)

        # Read noise
        read_noise_norm = (self.params.read_noise_e * self.analog_gain) / self.params.full_well
        noisy = noisy + torch.randn_like(noisy) * read_noise_norm

        # Clip and apply gamma
        noisy = torch.clamp(noisy, 0, 1)
        gamma_corrected = torch.pow(noisy, 1/2.2)

        return gamma_corrected


def realistic_sensor_noise_gpu(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    iso: int = 400,
    sensor_type: str = "smartphone",
    seed: Optional[int] = None
):
    """
    Apply GPU-accelerated realistic sensor noise to video.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Strength multiplier (0.5 = subtle, 1.0 = normal, 2.0 = heavy)
        iso: Simulated ISO level
        sensor_type: 'smartphone', 'apsc', 'fullframe', or 'webcam'
        seed: Random seed for reproducible FPN

    Raises:
        RuntimeError: If processing fails
    """
    # Map string to enum
    sensor_map = {
        'smartphone': SensorType.SMARTPHONE,
        'apsc': SensorType.APSC,
        'fullframe': SensorType.FULLFRAME,
        'webcam': SensorType.WEBCAM,
    }
    sensor_enum = sensor_map.get(sensor_type, SensorType.SMARTPHONE)

    # Adjust ISO by strength
    effective_iso = int(iso * strength)

    if GPU_UTILS_AVAILABLE and torch.cuda.is_available():
        device_info = get_device_info()
        print(f"[SensorNoise] Using CUDA GPU: {device_info['name']}")
        use_gpu = True
    else:
        print("[SensorNoise] Using CPU fallback")
        use_gpu = False

    # Open video
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    fps = int(cap.get(cv2.CAP_PROP_FPS))
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
            print(f"[SensorNoise] Metadata correction: {total_frames_meta} -> {total_frames} frames")
    else:
        total_frames = total_frames_meta

    print(f"[SensorNoise] Processing {total_frames} frames, ISO={effective_iso}, sensor={sensor_type}")

    # Setup video writer
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video {output_path}")

    if use_gpu:
        noise_gen = RealisticSensorNoiseGPU(
            iso=effective_iso,
            sensor_type=sensor_enum,
            device='cuda',
            seed=seed
        )
        batch_size = 8
        frame_buffer = []

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                # Convert to tensor
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame_tensor = torch.from_numpy(frame_rgb).permute(2, 0, 1).float() / 255.0
                frame_buffer.append(frame_tensor)

                if len(frame_buffer) >= batch_size:
                    batch = torch.stack(frame_buffer).to(noise_gen.device)
                    noisy_batch = noise_gen.add_noise_batch(batch)

                    for i in range(noisy_batch.shape[0]):
                        noisy_np = (noisy_batch[i].cpu().permute(1, 2, 0).numpy() * 255).astype(np.uint8)
                        noisy_bgr = cv2.cvtColor(noisy_np, cv2.COLOR_RGB2BGR)
                        out.write(noisy_bgr)

                    frame_buffer = []

            # Process remaining frames
            if frame_buffer:
                batch = torch.stack(frame_buffer).to(noise_gen.device)
                noisy_batch = noise_gen.add_noise_batch(batch)

                for i in range(noisy_batch.shape[0]):
                    noisy_np = (noisy_batch[i].cpu().permute(1, 2, 0).numpy() * 255).astype(np.uint8)
                    noisy_bgr = cv2.cvtColor(noisy_np, cv2.COLOR_RGB2BGR)
                    out.write(noisy_bgr)

        finally:
            cap.release()
            out.release()

    else:
        noise_gen = RealisticSensorNoise(
            iso=effective_iso,
            sensor_type=sensor_enum,
            seed=seed
        )

        frame_idx = 0
        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    break

                noisy_frame = noise_gen.add_noise(frame)
                out.write(noisy_frame)

                frame_idx += 1
                if frame_idx % 100 == 0:
                    print(f"[SensorNoise] Processed {frame_idx}/{total_frames} frames...")

        finally:
            cap.release()
            out.release()

    print(f"[SensorNoise] Completed: {output_path}")


def realistic_sensor_noise(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    iso: int = 400,
    sensor_type: str = "smartphone",
    seed: Optional[int] = None
):
    """
    Apply realistic camera sensor noise simulation.

    Main entry point for sensor noise injection that mimics real camera characteristics.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Effect strength (0.3-1.0 recommended)
                  - 0.3: Subtle, high quality camera
                  - 0.5: Default, typical smartphone
                  - 1.0: Noisy, low-light conditions
        iso: Base ISO level (100-6400)
             Higher values produce more noise
        sensor_type: Camera type to simulate
                    'smartphone', 'apsc', 'fullframe', 'webcam'
        seed: Random seed for consistent FPN across runs
    """
    realistic_sensor_noise_gpu(input_path, output_path, strength, iso, sensor_type, seed)


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 3:
        print("Usage: python sensor_noise.py <input_video> <output_video> [strength] [iso] [sensor_type]")
        print("  strength: Effect intensity 0-1 (default: 0.5)")
        print("  iso: ISO level 100-6400 (default: 400)")
        print("  sensor_type: smartphone/apsc/fullframe/webcam (default: smartphone)")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    iso = int(sys.argv[4]) if len(sys.argv) > 4 else 400
    sensor_type = sys.argv[5] if len(sys.argv) > 5 else "smartphone"

    realistic_sensor_noise(sys.argv[1], sys.argv[2], strength, iso, sensor_type)
