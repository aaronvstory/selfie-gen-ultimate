"""
Metadata stripping attack module.
Removes metadata from video files to evade metadata-based detection.
"""

import subprocess
import os


def strip_metadata(input_path: str, output_path: str):
    """
    Strip metadata from video file using ffmpeg.
    
    Args:
        input_path: Path to input video file
        output_path: Path to output video file
    """
    # Check if ffmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg.")
    
    # Use ffmpeg to copy video without metadata
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-map_metadata', '-1',  # Remove all metadata
        '-c:v', 'copy',  # Copy video codec
        '-c:a', 'copy',  # Copy audio codec
        '-y',  # Overwrite output file
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    
    print(f"Metadata stripped from {input_path} -> {output_path}")


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) != 3:
        print("Usage: python metadata_stripper.py <input_video> <output_video>")
        sys.exit(1)
    
    strip_metadata(sys.argv[1], sys.argv[2])
