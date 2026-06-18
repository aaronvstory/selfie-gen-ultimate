"""
GPU utility module for CUDA detection and device management.
"""

import torch
import numpy as np


def check_cuda_availability():
    """Check CUDA availability and print detailed status."""
    print("="*60)
    print("CUDA GPU Availability Check")
    print("="*60)
    
    cuda_available = torch.cuda.is_available()
    print(f"CUDA available: {cuda_available}")
    
    if cuda_available:
        print(f"CUDA device count: {torch.cuda.device_count()}")
        for i in range(torch.cuda.device_count()):
            print(f"  Device {i}: {torch.cuda.get_device_name(i)}")
            memory_gb = torch.cuda.get_device_properties(i).total_memory / 1e9
            print(f"    Memory: {memory_gb:.2f} GB")
    else:
        print("\nPossible reasons CUDA is not available:")
        print("1. No NVIDIA GPU present")
        print("2. NVIDIA drivers not installed")
        print("3. CUDA toolkit not installed")
        print("4. PyTorch was installed without CUDA support")
        print("5. Running in container without GPU passthrough")
    
    print("="*60)
    return cuda_available


def get_device(prefer_gpu=True):
    """Get the best available device (CUDA GPU or CPU)."""
    if prefer_gpu and torch.cuda.is_available():
        device = torch.device('cuda:0')
        device_name = torch.cuda.get_device_name(0)
        reason = "CUDA GPU available"
        print(f"Using CUDA GPU: {device_name}")
        return device, device_name, reason
    else:
        device = torch.device('cpu')
        device_name = 'CPU'
        
        if prefer_gpu:
            if not torch.cuda.is_available():
                reason = "CUDA not available. Check if NVIDIA drivers and CUDA are installed."
            else:
                reason = "GPU explicitly disabled"
        else:
            reason = "CPU explicitly requested"
        
        print(f"Using CPU: {reason}")
        return device, device_name, reason


def numpy_to_tensor(array, device=None):
    """Convert numpy array to PyTorch tensor on specified device."""
    if device is None:
        device, _, _ = get_device()
    
    tensor = torch.from_numpy(array).float()
    if device.type != 'cpu':
        tensor = tensor.to(device)
    
    return tensor


def tensor_to_numpy(tensor):
    """Convert PyTorch tensor to numpy array."""
    if tensor.is_cuda:
        tensor = tensor.cpu()
    return tensor.numpy()


def get_device_info():
    """Get device information as a dictionary for compatibility with GPU modules."""
    if torch.cuda.is_available():
        return {
            'name': torch.cuda.get_device_name(0),
            'device': 'cuda',
            'available': True
        }
    else:
        return {
            'name': 'CPU',
            'device': 'cpu',
            'available': False
        }


if __name__ == '__main__':
    check_cuda_availability()
    device, device_name, reason = get_device(prefer_gpu=True)
    print(f"Selected device: {device_name}")
    print(f"Reason: {reason}")
