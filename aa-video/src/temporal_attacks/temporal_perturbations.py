"""
Temporal perturbations attack module.
Adds temporally coherent perturbations to evade temporal-based detection.
"""

import cv2
import numpy as np
import os
from moviepy import VideoFileClip


def add_temporal_perturbations(input_path: str, output_path: str, strength: float = 0.1):
    """
    Add temporally coherent adversarial perturbations to video frames.
    
    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        strength: Strength of perturbations (0.0 to 1.0)
    """
    if not 0 <= strength <= 1:
        raise ValueError("Strength must be between 0.0 and 1.0")
    
    # Load video
    clip = VideoFileClip(input_path)
    fps = clip.fps
    
    # Process frames with temporal coherence
    processed_frames = []
    prev_perturbation = None
    
    for i, frame in enumerate(clip.iter_frames()):
        # Convert frame to float for processing
        frame_float = frame.astype(np.float32) / 255.0
        
        # Generate perturbation with temporal coherence
        if prev_perturbation is None:
            # First frame: random perturbation
            perturbation = np.random.randn(*frame_float.shape) * strength * 0.05
        else:
            # Subsequent frames: blend with previous perturbation for coherence
            new_perturbation = np.random.randn(*frame_float.shape) * strength * 0.05
            perturbation = 0.7 * prev_perturbation + 0.3 * new_perturbation
        
        # Apply perturbation
        perturbed_frame = frame_float + perturbation
        
        # Clip values to valid range
        perturbed_frame = np.clip(perturbed_frame, 0, 1)
        
        # Convert back to uint8
        perturbed_frame_uint8 = (perturbed_frame * 255).astype(np.uint8)
        
        processed_frames.append(perturbed_frame_uint8)
        prev_perturbation = perturbation
    
    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)
    
    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')
    
    print(f"Temporal perturbations added to {input_path} -> {output_path}")
    print(f"Perturbation strength: {strength}")


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python temporal_perturbations.py <input_video> <output_video> [strength]")
        sys.exit(1)
    
    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    
    add_temporal_perturbations(sys.argv[1], sys.argv[2], strength)
