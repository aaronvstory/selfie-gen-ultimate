"""
Watermark removal attack module.
Implements techniques to remove or degrade invisible watermarks from video.
"""

import cv2
import numpy as np
from moviepy import VideoFileClip
import subprocess
import os


def remove_watermark_regeneration(input_path: str, output_path: str, denoise_strength: float = 0.1):
    """
    Remove watermarks using regeneration/denoising approach.
    Based on research: "Invisible Image Watermarks Are Provably Removable" (Zhao et al., NeurIPS 2024).
    
    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        denoise_strength: Strength of denoising (0.0 to 1.0)
    """
    if not 0 <= denoise_strength <= 1:
        raise ValueError("Denoise strength must be between 0.0 and 1.0")
    
    # Load video
    clip = VideoFileClip(input_path)
    fps = clip.fps
    
    # Process frames
    processed_frames = []
    
    for frame in clip.iter_frames():
        # Convert frame to float for processing
        frame_float = frame.astype(np.float32) / 255.0
        
        # Apply denoising to remove watermark patterns
        # Simple approach: Gaussian blur to remove high-frequency watermark patterns
        kernel_size = int(denoise_strength * 10) + 1
        kernel_size = kernel_size if kernel_size % 2 == 1 else kernel_size + 1
        
        # Apply blur to each channel
        denoised_frame = np.zeros_like(frame_float)
        for i in range(3):  # RGB channels
            denoised_frame[:, :, i] = cv2.GaussianBlur(
                frame_float[:, :, i], 
                (kernel_size, kernel_size), 
                0
            )
        
        # Add slight noise to break watermark patterns
        noise = np.random.randn(*denoised_frame.shape) * denoise_strength * 0.01
        denoised_frame = denoised_frame + noise
        
        # Clip values to valid range
        denoised_frame = np.clip(denoised_frame, 0, 1)
        
        # Convert back to uint8
        denoised_frame_uint8 = (denoised_frame * 255).astype(np.uint8)
        
        processed_frames.append(denoised_frame_uint8)
    
    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)
    
    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')
    
    print(f"Watermark removal via regeneration applied: {input_path} -> {output_path}")
    print(f"Denoise strength: {denoise_strength}")


def apply_editing_transforms(input_path: str, output_path: str):
    """
    Apply generic editing transforms to degrade watermarks.
    Techniques: crop, resize, color shifts, recompression.
    """
    # Use ffmpeg to apply multiple transforms
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-vf', 'crop=iw-20:ih-20:10:10,scale=iw*0.95:ih*0.95',  # Crop and resize
        '-c:v', 'libx264',
        '-crf', '28',  # Higher CRF = more compression
        '-c:a', 'aac',
        '-b:a', '128k',
        '-y',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    
    print(f"Editing transforms applied: {input_path} -> {output_path}")


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python watermark_remover.py <input_video> <output_video> [denoise_strength]")
        sys.exit(1)
    
    if len(sys.argv) > 3:
        strength = float(sys.argv[3])
        remove_watermark_regeneration(sys.argv[1], sys.argv[2], strength)
    else:
        apply_editing_transforms(sys.argv[1], sys.argv[2])
