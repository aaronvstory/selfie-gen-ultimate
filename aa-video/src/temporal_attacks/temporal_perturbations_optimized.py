"""
GPU/CPU Optimized temporal perturbations attack module.
Uses vectorized operations and optimized temporal coherence.
"""

import cv2
import numpy as np
from moviepy import VideoFileClip


def add_temporal_perturbations_optimized(input_path: str, output_path: str, strength: float = 0.1, batch_size: int = 8):
    """
    Add temporally coherent adversarial perturbations with optimization.

    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        strength: Strength of perturbations (0.0 to 1.0)
        batch_size: Number of frames to process in temporal batches
    """
    if not 0 <= strength <= 1:
        raise ValueError("Strength must be between 0.0 and 1.0")

    # Load video
    clip = VideoFileClip(input_path)
    fps = clip.fps

    # Get all frames
    frames = list(clip.iter_frames())
    n_frames = len(frames)

    # Process frames with temporal coherence
    processed_frames = []

    # Initialize perturbation for first batch
    first_batch = frames[0:min(batch_size, n_frames)]
    first_batch_array = np.array(first_batch, dtype=np.float32) / 255.0

    # Generate initial perturbations
    noise_shape = (len(first_batch),) + first_batch_array.shape[1:]
    perturbations = np.random.randn(*noise_shape) * strength * 0.05

    # Apply temporal smoothing within batch
    for i in range(1, len(perturbations)):
        perturbations[i] = 0.7 * perturbations[i-1] + 0.3 * perturbations[i]

    # Apply perturbations
    perturbed_batch = first_batch_array + perturbations
    perturbed_batch = np.clip(perturbed_batch, 0, 1)
    perturbed_batch_uint8 = (perturbed_batch * 255).astype(np.uint8)
    processed_frames.extend(list(perturbed_batch_uint8))

    # Process remaining frames
    last_perturbation = perturbations[-1]

    for i in range(batch_size, n_frames, batch_size):
        batch = frames[i:i+batch_size]
        batch_array = np.array(batch, dtype=np.float32) / 255.0

        # Generate new perturbations with temporal coherence from last
        noise_shape = (len(batch),) + batch_array.shape[1:]
        new_perturbations = np.random.randn(*noise_shape) * strength * 0.05

        # Blend with last perturbation for temporal coherence
        for j in range(len(new_perturbations)):
            if j == 0:
                new_perturbations[j] = 0.7 * last_perturbation + 0.3 * new_perturbations[j]
            else:
                new_perturbations[j] = 0.7 * new_perturbations[j-1] + 0.3 * new_perturbations[j]

        # Apply perturbations
        perturbed_batch = batch_array + new_perturbations
        perturbed_batch = np.clip(perturbed_batch, 0, 1)
        perturbed_batch_uint8 = (perturbed_batch * 255).astype(np.uint8)
        processed_frames.extend(list(perturbed_batch_uint8))

        last_perturbation = new_perturbations[-1]

    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)

    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')

    print(f"Temporal perturbations (optimized) added to {input_path} -> {output_path}")
    print(f"Perturbation strength: {strength}, Batch size: {batch_size}")


# Keep original function for backward compatibility
def add_temporal_perturbations(input_path: str, output_path: str, strength: float = 0.1):
    """Original function, calls optimized version with default batch size."""
    return add_temporal_perturbations_optimized(input_path, output_path, strength, batch_size=8)


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python temporal_perturbations_optimized.py <input_video> <output_video> [strength] [batch_size]")
        sys.exit(1)

    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    batch_size = int(sys.argv[4]) if len(sys.argv) > 4 else 8

    add_temporal_perturbations_optimized(sys.argv[1], sys.argv[2], strength, batch_size)
