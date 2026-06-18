"""
GPU/CPU Optimized model fingerprint evasion module.
Based on TraceEvader research with vectorized operations.
"""

import cv2
import numpy as np
from moviepy import VideoFileClip


def evade_traceevader_optimized(input_path: str, output_path: str, strength: float = 0.1, batch_size: int = 16):
    """
    Evade model attribution using frequency-domain attacks with optimization.

    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        strength: Attack strength (0.0 to 1.0)
        batch_size: Number of frames to process at once
    """
    if not 0 <= strength <= 1:
        raise ValueError("Strength must be between 0.0 and 1.0")

    # Load video
    clip = VideoFileClip(input_path)
    fps = clip.fps

    # Get all frames
    frames = list(clip.iter_frames())
    n_frames = len(frames)

    # Process frames in batches
    processed_frames = []

    for i in range(0, n_frames, batch_size):
        batch = frames[i:i+batch_size]
        batch_array = np.array(batch, dtype=np.float32) / 255.0

        # Vectorized frequency-domain manipulation
        # 1. Apply blur to low frequencies
        kernel_size = int(strength * 5) + 1
        kernel_size = kernel_size if kernel_size % 2 == 1 else kernel_size + 1

        # Process each channel separately
        processed_batch = np.zeros_like(batch_array)

        for ch in range(3):  # RGB channels
            # Vectorized Gaussian blur using OpenCV
            for j in range(len(batch_array)):
                processed_batch[j, :, :, ch] = cv2.GaussianBlur(
                    batch_array[j, :, :, ch],
                    (kernel_size, kernel_size),
                    0
                )

        # 2. Add high-frequency noise
        hf_noise = np.random.randn(*batch_array.shape) * strength * 0.02

        # Combine: more blur in low frequencies, noise in high frequencies
        result = 0.7 * processed_batch + 0.3 * (batch_array + hf_noise)
        result = np.clip(result, 0, 1)
        result_uint8 = (result * 255).astype(np.uint8)
        processed_frames.extend(list(result_uint8))

    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)

    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')

    print(f"TraceEvader attribution evasion (optimized) applied: {input_path} -> {output_path}")
    print(f"Attack strength: {strength}, Batch size: {batch_size}")


# Keep original function for backward compatibility
def evade_traceevader(input_path: str, output_path: str, strength: float = 0.1):
    """Original function, calls optimized version with default batch size."""
    return evade_traceevader_optimized(input_path, output_path, strength, batch_size=16)


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python trace_evader_optimized.py <input_video> <output_video> [strength] [batch_size]")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    batch_size = int(sys.argv[4]) if len(sys.argv) > 4 else 16

    evade_traceevader_optimized(sys.argv[1], sys.argv[2], strength, batch_size)
