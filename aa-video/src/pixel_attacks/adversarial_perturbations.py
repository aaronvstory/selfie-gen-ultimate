"""
Adversarial perturbations attack module.
Adds subtle perturbations to video frames to evade AI detection.
"""

import cv2
import numpy as np
import os
from moviepy import VideoFileClip


def add_adversarial_perturbations(input_path: str, output_path: str, strength: float = 0.1):
    """
    Add adversarial perturbations to video frames.
    
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
    
    # Process frames
    processed_frames = []
    
    for frame in clip.iter_frames():
        # Convert frame to float for processing
        frame_float = frame.astype(np.float32) / 255.0
        
        # Generate adversarial perturbation
        # Simple approach: add high-frequency noise
        noise = np.random.randn(*frame_float.shape) * strength * 0.1
        
        # Apply perturbation
        perturbed_frame = frame_float + noise
        
        # Clip values to valid range
        perturbed_frame = np.clip(perturbed_frame, 0, 1)
        
        # Convert back to uint8
        perturbed_frame_uint8 = (perturbed_frame * 255).astype(np.uint8)
        
        processed_frames.append(perturbed_frame_uint8)
    
    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)
    
    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')
    
    print(f"Adversarial perturbations added to {input_path} -> {output_path}")
    print(f"Perturbation strength: {strength}")


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python adversarial_perturbations.py <input_video> <output_video> [strength]")
        sys.exit(1)
    
    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    
    add_adversarial_perturbations(sys.argv[1], sys.argv[2], strength)
