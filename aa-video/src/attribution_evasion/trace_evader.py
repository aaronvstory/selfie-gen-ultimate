"""
Model fingerprint evasion module.
Implements techniques to evade model attribution/fingerprint detection.
Based on TraceEvader research (Wu et al. 2024).
"""

import cv2
import numpy as np
from moviepy import VideoFileClip
import subprocess
import os


def evade_traceevader(input_path: str, output_path: str, strength: float = 0.1):
    """
    Evade model attribution using frequency-domain attacks.
    Based on TraceEvader: imitates traces in high-frequency components + adversarial blur.
    
    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        strength: Attack strength (0.0 to 1.0)
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
        
        # Apply frequency-domain manipulation
        # 1. Apply slight blur to low frequencies
        kernel_size = int(strength * 5) + 1
        kernel_size = kernel_size if kernel_size % 2 == 1 else kernel_size + 1
        
        blurred = np.zeros_like(frame_float)
        for i in range(3):  # RGB channels
            blurred[:, :, i] = cv2.GaussianBlur(
                frame_float[:, :, i], 
                (kernel_size, kernel_size), 
                0
            )
        
        # 2. Add high-frequency noise to imitate traces
        hf_noise = np.random.randn(*frame_float.shape) * strength * 0.02
        
        # 3. Combine: more blur in low frequencies, noise in high frequencies
        # Simple approach: weighted combination
        result = 0.7 * blurred + 0.3 * (frame_float + hf_noise)
        
        # Clip values to valid range
        result = np.clip(result, 0, 1)
        
        # Convert back to uint8
        result_uint8 = (result * 255).astype(np.uint8)
        
        processed_frames.append(result_uint8)
    
    # Create output video
    from moviepy import ImageSequenceClip
    output_clip = ImageSequenceClip(processed_frames, fps=fps)
    
    # Write output
    output_clip.write_videofile(output_path, codec='libx264', audio_codec='aac')
    
    print(f"TraceEvader attribution evasion applied: {input_path} -> {output_path}")
    print(f"Attack strength: {strength}")


def apply_frequency_smoothing(input_path: str, output_path: str):
    """
    Apply frequency-domain smoothing to remove generator fingerprints.
    """
    # Use ffmpeg with frequency-domain filters
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-vf', 'noise=c0s=8:c0f=t',  # Add temporal noise
        '-c:v', 'libx264',
        '-crf', '25',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-y',
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    
    print(f"Frequency smoothing applied: {input_path} -> {output_path}")


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python trace_evader.py <input_video> <output_video> [strength]")
        sys.exit(1)
    
    if len(sys.argv) > 3:
        strength = float(sys.argv[3])
        evade_traceevader(sys.argv[1], sys.argv[2], strength)
    else:
        apply_frequency_smoothing(sys.argv[1], sys.argv[2])
