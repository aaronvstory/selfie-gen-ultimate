"""GPU-accelerated temporal perturbations without MoviePy dependency."""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from gpu_utils import get_device_info

def temporal_perturbations_gpu(input_path, output_path, strength=0.3, noise_level=0.01):
    """
    Apply GPU-accelerated temporal perturbations using OpenCV instead of MoviePy.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Strength of optical flow perturbations
        noise_level: Level of temporal noise to add

    Raises:
        RuntimeError: If CUDA is not available or processing fails
    """
    device_info = get_device_info()
    print(f"Using CUDA GPU: {device_info['name']}")

    # Check if CUDA is available
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA not available for GPU acceleration")

    device = torch.device('cuda')

    # Open video with OpenCV
    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video {input_path}")

    # Get video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    print(f"Processing {total_frames} frames with GPU acceleration on {device_info['name']}")

    # Setup video writer (use H.264 codec for better compatibility)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

    if not out.isOpened():
        cap.release()
        raise RuntimeError(f"Could not create output video {output_path}")

    # Read first frame
    ret, prev_frame = cap.read()
    if not ret:
        cap.release()
        out.release()
        raise RuntimeError("Could not read first frame")

    # Convert first frame to tensor and move to GPU
    prev_tensor = torch.from_numpy(prev_frame).float().permute(2, 0, 1).unsqueeze(0).to(device)

    # Write first frame
    out.write(prev_frame)

    frame_count = 1

    try:
        while True:
            ret, curr_frame = cap.read()
            if not ret:
                break

            # Convert current frame to tensor
            curr_tensor = torch.from_numpy(curr_frame).float().permute(2, 0, 1).unsqueeze(0).to(device)

            # Apply temporal perturbation using optical flow
            perturbed_tensor = apply_temporal_perturbation(prev_tensor, curr_tensor, strength, noise_level)

            # Convert back to numpy and write frame (detach to remove gradients)
            perturbed_numpy = perturbed_tensor.squeeze(0).permute(1, 2, 0).detach().cpu().numpy().astype(np.uint8)
            out.write(perturbed_numpy)

            # Update previous frame for next iteration
            prev_tensor = curr_tensor.clone()

            frame_count += 1
            if frame_count % 50 == 0:
                print(f"Processed {frame_count}/{total_frames} frames...")
    finally:
        cap.release()
        out.release()

    print(f"GPU-accelerated temporal perturbations completed: {output_path}")

def apply_temporal_perturbation(prev_tensor, curr_tensor, flow_strength=0.1, noise_level=0.01):
    """
    Apply temporal perturbation using optical flow and noise.
    
    Args:
        prev_tensor: Previous frame tensor (1, C, H, W)
        curr_tensor: Current frame tensor (1, C, H, W)  
        flow_strength: Strength of flow-based perturbation
        noise_level: Level of temporal noise
    
    Returns:
        torch.Tensor: Perturbed current frame tensor
    """
    device = prev_tensor.device
    
    # Calculate difference between frames (motion)
    motion = curr_tensor - prev_tensor
    
    # Add controlled temporal noise
    noise = torch.randn_like(curr_tensor) * noise_level
    
    # Apply flow-based perturbation (simplified)
    perturbed = curr_tensor + flow_strength * motion + noise
    
    # Clamp to valid range
    perturbed = torch.clamp(perturbed, 0, 255)
    
    return perturbed
