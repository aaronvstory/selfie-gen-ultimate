"""
GPU/CPU Optimized adversarial perturbations attack module.
Uses vectorized operations and batch processing for speed.
"""

import cv2
import numpy as np
from moviepy import VideoFileClip
import multiprocessing as mp
from functools import partial


def add_adversarial_perturbations_optimized(input_path: str, output_path: str, strength: float = 0.1, batch_size: int = 16):
    """
    Add adversarial perturbations to video frames with optimization.

    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        strength: Strength of perturbations (0.0 to 1.0)
        batch_size: Number of frames to process at once
    """
    if not 0 <= strength <= 1:
        raise ValueError("Strength must be between 0.0 and 1.0")

    # Load video
    clip = VideoFileClip(input_path)
    fps = clip.fps

    # Get all frames as a list
    frames = list(clip.iter_frames())
    n_frames = len(frames)

    # Process frames in batches
    processed_frames = []

    for i in range(0, n_frames, batch_size):
        batch = frames[i:i+batch_size]
        batch_array = np.array(batch, dtype=np.float32) / 255.0

        # Vectorized noise generation
        noise_shape = (len(batch),) + batch_array.shape[1:]
        noise = np.random.randn(*noise_shape) * strength * 0.1

        # Vectorized perturbation
        perturbed_batch = batch_array + noise
        perturbed_batch = np.clip(perturbed_batch, 0, 1)

        # Convert back to uint8
        perturbed_batch_uint8 = (perturbed_batch * 255).astype(np.uint8)

        processed_frames.extend(list(perturbed_batch_uint8))

    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)

    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')

    print(f"Adversarial perturbations (optimized) added to {input_path} -> {output_path}")
    print(f"Perturbation strength: {strength}, Batch size: {batch_size}")


# Keep original function for backward compatibility
def add_adversarial_perturbations(input_path: str, output_path: str, strength: float = 0.1):
    """Original function, calls optimized version with default batch size."""
    return add_adversarial_perturbations_optimized(input_path, output_path, strength, batch_size=16)


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python adversarial_perturbations_optimized.py <input_video> <output_video> [strength] [batch_size]")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    batch_size = int(sys.argv[4]) if len(sys.argv) > 4 else 16

    add_adversarial_perturbations_optimized(sys.argv[1], sys.argv[2], strength, batch_size)
