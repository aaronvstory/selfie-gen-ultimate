"""
Single-pass recompression attack module.
Re-encodes video with a single pass to remove double-compression artifacts.
"""

import subprocess
import os


def single_pass_recompress(input_path: str, output_path: str, codec: str = 'libx264', crf: int = 23):
    """
    Re-encode video with single pass compression.
    
    Args:
        input_path: Path to input video file
        output_path: Path to output video file
        codec: Video codec to use (default: libx264)
        crf: Constant Rate Factor quality (lower = better quality, default: 23)
    """
    # Check if ffmpeg is available
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg.")
    
    # Use ffmpeg to re-encode video with single pass
    cmd = [
        'ffmpeg',
        '-i', input_path,
        '-c:v', codec,
        '-crf', str(crf),
        '-preset', 'medium',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-y',  # Overwrite output file
        output_path
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")
    
    print(f"Video recompressed: {input_path} -> {output_path}")
    print(f"Codec: {codec}, CRF: {crf}")


if __name__ == '__main__':
    # Test function
    import sys
    if len(sys.argv) < 3:
        print("Usage: python recompression.py <input_video> <output_video> [codec] [crf]")
        sys.exit(1)
    
    codec = sys.argv[3] if len(sys.argv) > 3 else 'libx264'
    crf = int(sys.argv[4]) if len(sys.argv) > 4 else 23
    
    single_pass_recompress(sys.argv[1], sys.argv[2], codec, crf)
