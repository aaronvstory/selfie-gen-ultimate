"""
Chunked Alternating Attack - Temporal Confusion Strategy

This module implements a sophisticated anti-detection approach by:
1. Splitting video into random-length chunks (19-33 frames)
2. Alternating between scenario1 (DSP-FWA/FTCN) and scenario3 (AltFreezing) attacks
3. Blending overlapping frames to smooth transitions

This creates temporal inconsistency patterns that confuse detectors expecting
uniform attack signatures across the entire video.

Target Detectors: All (temporal confusion strategy)
"""

import os
import sys
import cv2
import numpy as np
import tempfile
import random
from typing import List, Tuple, Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def extract_video_frames(video_path: str, max_frames: int = 50000) -> Tuple[List[np.ndarray], float, Tuple[int, int]]:
    """
    Extract all frames from video.

    Args:
        video_path: Path to video file
        max_frames: Maximum frames to load (safety limit to prevent OOM)

    Returns:
        frames: List of frame arrays
        fps: Frame rate
        size: (width, height)

    Raises:
        RuntimeError: If video cannot be opened or exceeds max_frames
    """
    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if fps <= 0 or width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError(f"Invalid video properties: fps={fps}, size={width}x{height}")

    frames = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frames.append(frame)

        if len(frames) > max_frames:
            cap.release()
            raise RuntimeError(f"Video exceeds safety limit of {max_frames} frames. Use a shorter video or increase max_frames.")

    cap.release()

    if len(frames) == 0:
        raise RuntimeError(f"No frames extracted from video: {video_path}")

    return frames, fps, (width, height)


def write_frames_to_video(frames: List[np.ndarray], output_path: str, fps: float, size: Tuple[int, int]):
    """Write frames to video file with proper frame count."""
    # Use mp4v codec (reliable on Windows)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, size)

    if not out.isOpened():
        raise RuntimeError(f"Failed to create video writer for {output_path}")

    for frame in frames:
        out.write(frame)

    out.release()

    # Verify the written video has correct frame count
    cap = cv2.VideoCapture(output_path)
    actual_frames = 0
    while True:
        ret, _ = cap.read()
        if not ret:
            break
        actual_frames += 1
    cap.release()

    if actual_frames != len(frames):
        print(f"[WARN] Frame count mismatch: wrote {len(frames)}, video has {actual_frames}")

    return actual_frames


def blend_frames(frame1: np.ndarray, frame2: np.ndarray, alpha: float) -> np.ndarray:
    """
    Blend two frames with given alpha.

    Args:
        frame1: First frame
        frame2: Second frame
        alpha: Blend factor (0.0 = all frame1, 1.0 = all frame2)

    Returns:
        Blended frame
    """
    return cv2.addWeighted(frame1, 1 - alpha, frame2, alpha, 0)


def apply_attack_to_chunk(
    chunk_frames: List[np.ndarray],
    attack_type: str,
    temp_dir: str,
    fps: float,
    size: Tuple[int, int],
    strength: float,
    generator: Optional[str] = None
) -> List[np.ndarray]:
    """
    Apply scenario1 or scenario3 attack to a chunk of frames.

    Args:
        chunk_frames: List of frames for this chunk
        attack_type: 'scenario1' or 'scenario3'
        temp_dir: Temporary directory for intermediate files
        fps: Frame rate
        size: (width, height)
        strength: Attack strength
        generator: Generator profile name

    Returns:
        Attacked frames
    """
    import subprocess
    import time

    # Write chunk to temporary video
    chunk_input = os.path.join(temp_dir, f'chunk_input_{random.randint(10000, 99999)}.mp4')
    chunk_output = os.path.join(temp_dir, f'chunk_output_{random.randint(10000, 99999)}.mp4')

    try:
        write_frames_to_video(chunk_frames, chunk_input, fps, size)

        # Verify file was written and is readable
        if not os.path.exists(chunk_input):
            raise RuntimeError(f"Failed to write chunk input file: {chunk_input}")

        # Build command to run attack
        main_script = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'main.py'))
        cmd = [
            sys.executable,
            main_script,
            '--input', chunk_input,
            '--attack', attack_type,
            '--strength', str(strength),
            '--output', chunk_output
        ]

        if generator:
            cmd.extend(['--generator', generator])

        # Run attack with timeout (10 minutes max per chunk)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        if result.returncode != 0:
            print(f"[WARN] Chunk attack failed: {result.stderr}")
            return chunk_frames

        # Read attacked frames
        attacked_frames, _, _ = extract_video_frames(chunk_output, max_frames=100)

        # Reconcile frame count differences (frame_chaos may drop/duplicate frames)
        if len(attacked_frames) != len(chunk_frames):
            print(f"[WARN] Frame count mismatch: input={len(chunk_frames)}, output={len(attacked_frames)}, reconciling...")

            if len(attacked_frames) > len(chunk_frames):
                # Too many frames - trim from end
                attacked_frames = attacked_frames[:len(chunk_frames)]
                print(f"[INFO] Trimmed {len(attacked_frames) - len(chunk_frames)} excess frames")

            elif len(attacked_frames) < len(chunk_frames):
                # Too few frames - duplicate last frame to pad
                frames_needed = len(chunk_frames) - len(attacked_frames)
                for _ in range(frames_needed):
                    attacked_frames.append(attacked_frames[-1].copy())
                print(f"[INFO] Padded {frames_needed} frames by duplicating last frame")

        return attacked_frames

    except subprocess.TimeoutExpired:
        print(f"[WARN] Chunk attack timed out after 600s")
        return chunk_frames
    except Exception as e:
        print(f"[WARN] Chunk attack error: {e}")
        return chunk_frames
    finally:
        # Always clean up temp files
        for path in [chunk_input, chunk_output]:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError as e:
                print(f"[WARN] Failed to remove temp file {path}: {e}")


def alternating_chunked_attack(
    input_path: str,
    output_path: str,
    strength: float = 0.5,
    generator: Optional[str] = None,
    seed: Optional[int] = None
):
    """
    Apply alternating chunked attack to video.

    Args:
        input_path: Input video path
        output_path: Output video path
        strength: Attack strength (0.1-1.0)
        generator: Generator profile name
        seed: Random seed for reproducibility
    """
    if seed is not None:
        random.seed(seed)
        np.random.seed(seed)

    print(f"[ChunkedAlternating] Processing video: {input_path}")
    print(f"[ChunkedAlternating] Strength: {strength}")
    if generator:
        print(f"[ChunkedAlternating] Generator: {generator}")

    # Extract frames
    print(f"[ChunkedAlternating] Extracting frames...")
    frames, fps, size = extract_video_frames(input_path)
    total_frames = len(frames)
    print(f"[ChunkedAlternating] Total frames: {total_frames}, FPS: {fps:.2f}")

    # Create temp directory (will be cleaned up in finally block)
    temp_dir = tempfile.mkdtemp(prefix='chunked_attack_')

    try:
        # Process chunks
        result_frames = []
        current_idx = 0
        chunk_num = 0
        attack_toggle = 0  # 0 = scenario1, 1 = scenario3

        print(f"[ChunkedAlternating] Starting chunked processing...")

        while current_idx < total_frames:
            # Determine chunk length
            frames_remaining = total_frames - current_idx

            if frames_remaining < 19:
                # Use all remaining frames
                chunk_length = frames_remaining
            else:
                # Random length between 19 and 33
                chunk_length = random.randint(19, min(33, frames_remaining))

            # Extract chunk (including overlap if not first chunk)
            if current_idx == 0:
                # First chunk - no leading overlap
                chunk_start = 0
                chunk_end = chunk_length
                overlap_start = False
            else:
                # Include 2-frame overlap at start
                chunk_start = max(0, current_idx - 2)
                chunk_end = min(total_frames, current_idx + chunk_length)
                overlap_start = True

            chunk_frames = frames[chunk_start:chunk_end]

            # Determine attack type
            attack_type = 'scenario1' if attack_toggle == 0 else 'scenario3'
            attack_toggle = 1 - attack_toggle  # Toggle for next chunk

            chunk_num += 1
            print(f"[ChunkedAlternating] Chunk {chunk_num}: frames {chunk_start}-{chunk_end} ({len(chunk_frames)} frames), attack: {attack_type}")

            # Apply attack to chunk
            attacked_chunk = apply_attack_to_chunk(
                chunk_frames,
                attack_type,
                temp_dir,
                fps,
                size,
                strength,
                generator
            )

            # Handle blending with bounds checking
            if current_idx == 0:
                # First chunk - add all frames
                result_frames.extend(attacked_chunk)
            else:
                # Blend overlap region (2 frames at start)
                overlap_frames = 2

                # Validate we have enough frames for blending
                if len(result_frames) >= overlap_frames and len(attacked_chunk) >= overlap_frames:
                    # Replace last 2 frames of result with blended versions
                    for i in range(overlap_frames):
                        # Blend: gradually transition from previous to current
                        alpha = (i + 1) / (overlap_frames + 1)
                        blended = blend_frames(
                            result_frames[-(overlap_frames - i)],
                            attacked_chunk[i],
                            alpha
                        )
                        result_frames[-(overlap_frames - i)] = blended

                    # Add remaining frames from attacked chunk (skip overlap)
                    result_frames.extend(attacked_chunk[overlap_frames:])
                else:
                    # Not enough frames for blending, just concatenate
                    print(f"[WARN] Insufficient frames for blending, concatenating instead")
                    result_frames.extend(attacked_chunk)

            # Move to next chunk
            current_idx += chunk_length

        print(f"[ChunkedAlternating] Processed {chunk_num} chunks")
        print(f"[ChunkedAlternating] Result frames: {len(result_frames)}")

        # Write output video
        print(f"[ChunkedAlternating] Writing output: {output_path}")
        write_frames_to_video(result_frames, output_path, fps, size)

        print(f"[ChunkedAlternating] Complete!")

    finally:
        # Always clean up temp directory
        try:
            import shutil
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except OSError as e:
            print(f"[WARN] Failed to remove temp directory {temp_dir}: {e}")


if __name__ == '__main__':
    # Test mode
    import sys
    if len(sys.argv) < 3:
        print("Usage: python alternating_chunked.py <input> <output> [strength] [generator]")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]
    strength = float(sys.argv[3]) if len(sys.argv) > 3 else 0.5
    generator = sys.argv[4] if len(sys.argv) > 4 else None

    alternating_chunked_attack(input_path, output_path, strength, generator)
