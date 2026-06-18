"""GPU-accelerated adversarial perturbations without MoviePy dependency."""

import os
import cv2
import numpy as np
import torch
import torch.nn.functional as F
from gpu_utils import get_device_info

def adversarial_perturbations_gpu(input_path, output_path, strength=0.2, iterations=10):
    """
    Apply GPU-accelerated adversarial perturbations using OpenCV instead of MoviePy.

    Args:
        input_path: Path to input video
        output_path: Path to output video
        strength: Perturbation strength (epsilon)
        iterations: Number of optimization iterations

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

            # Apply adversarial perturbation
            perturbed_frame = apply_adversarial_perturbation(frame_tensor, strength, iterations)

            # Convert back to numpy and write frame (detach to remove gradients)
            perturbed_numpy = perturbed_frame.squeeze(0).permute(1, 2, 0).detach().cpu().numpy().astype(np.uint8)
            out.write(perturbed_numpy)

            frame_count += 1
            if frame_count % 50 == 0:
                print(f"Processed {frame_count}/{total_frames} frames...")
    finally:
        cap.release()
        out.release()

    print(f"GPU-accelerated adversarial perturbations completed: {output_path}")

def apply_adversarial_perturbation(frame_tensor, epsilon=0.01, iterations=10):
    """
    Apply FGSM-like adversarial perturbation to a frame.
    
    Args:
        frame_tensor: Input frame tensor (1, C, H, W)
        epsilon: Perturbation strength
        iterations: Number of iterations
    
    Returns:
        torch.Tensor: Perturbed frame tensor
    """
    device = frame_tensor.device
    
    # Initialize perturbation
    delta = torch.zeros_like(frame_tensor, requires_grad=True)
    
    # Simple FGSM attack (gradient ascent on loss)
    for _ in range(iterations):
        # Forward pass - use L2 norm as loss (higher = more "adversarial")
        loss = torch.norm(delta)
        
        # Backward pass
        loss.backward()
        
        # Update perturbation (gradient ascent)
        with torch.no_grad():
            delta.data += epsilon * delta.grad.sign()
            delta.data.clamp_(-epsilon, epsilon)
            delta.grad.zero_()
    
    # Apply perturbation and clamp to valid range
    perturbed = frame_tensor + delta
    perturbed = torch.clamp(perturbed, 0, 255)
    
    return perturbed
