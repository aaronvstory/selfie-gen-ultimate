"""GPU-accelerated trace evasion without MoviePy dependency."""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from gpu_utils import get_device_info

def trace_evader_gpu(input_path, output_path, strength=0.2, blur_strength=0.08):
    """
    Apply GPU-accelerated trace evasion using OpenCV instead of MoviePy.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Strength of frequency domain perturbations
        blur_strength: Strength of blur-based trace removal

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

    frame_count = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Convert frame to tensor and move to GPU
            frame_tensor = torch.from_numpy(frame).float().permute(2, 0, 1).unsqueeze(0).to(device)

            # Apply trace evasion using frequency domain perturbations
            evaded_frame = apply_trace_evasion(frame_tensor, strength, blur_strength)

            # Convert back to numpy and write frame (detach to remove gradients)
            evaded_numpy = evaded_frame.squeeze(0).permute(1, 2, 0).detach().cpu().numpy().astype(np.uint8)
            out.write(evaded_numpy)

            frame_count += 1
            if frame_count % 50 == 0:
                print(f"Processed {frame_count}/{total_frames} frames...")
    finally:
        cap.release()
        out.release()

    print(f"GPU-accelerated trace evasion completed: {output_path}")

def apply_trace_evasion(frame_tensor, frequency_strength=0.1, blur_strength=0.05):
    """
    Apply trace evasion using frequency domain perturbations and blur.
    
    Args:
        frame_tensor: Input frame tensor (1, C, H, W)
        frequency_strength: Strength of frequency perturbations
        blur_strength: Strength of blur for trace removal
    
    Returns:
        torch.Tensor: Evaded frame tensor
    """
    device = frame_tensor.device
    
    # Apply frequency domain perturbations (simplified)
    # Convert to frequency domain using FFT
    fft_frame = torch.fft.fft2(frame_tensor)
    
    # Add random perturbations in frequency domain
    noise = torch.randn_like(fft_frame) * frequency_strength
    perturbed_fft = fft_frame + noise
    
    # Convert back to spatial domain
    evaded_frame = torch.fft.ifft2(perturbed_fft).real
    
    # Apply blur for additional trace removal (simplified)
    # Use a simple smoothing kernel in frequency domain
    kernel_size = int(blur_strength * 10) + 1  # Ensure odd number
    if kernel_size % 2 == 0:
        kernel_size += 1
    
    # Simple blur approximation using average pooling
    evaded_frame = F.avg_pool2d(evaded_frame, kernel_size=3, stride=1, padding=1)
    
    # Clamp to valid range and resize back to original size if needed
    evaded_frame = torch.clamp(evaded_frame, 0, 255)
    
    return evaded_frame
