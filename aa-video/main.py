#!/usr/bin/env python3
"""
Adversarial Video Attack Toolkit v3.0 - GPU-Accelerated
Main CLI interface for applying various adversarial attacks to video content.

v3.0 adds new modules targeting specific detector combos:
- Scenario #1 (DSP-FWA + FTCN): Replay/pre-recorded detection evasion
- Scenario #3 (AltFreezing): Smoothing/puppeteering detection evasion

v3.1 adds generator-specific attack profiles:
- Tailored parameters for Bytedance Seedance, Kling AI, RunwayML
- Preserves native strengths, adds only complementary perturbations
- Based on detector feedback analysis (2026)

New attacks:
- micro_jitter: Sub-pixel motion injection
- analog_sim: Analog capture chain simulation
- camera_recapture: Camera-in-camera effects
- sensor_noise: Realistic camera sensor noise
- motion_mod: Motion energy modulation
- frame_chaos: Variable frame rate / timing jitter
"""

import os
import sys
import argparse
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------
# Path setup – make sure the project's src directory is on the import path.
# ---------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

# Import generator profile system
from generator_profiles import get_profile, get_attack_strength, print_profile_info, list_generators, list_all_generator_aliases

# ---------------------------------------------------------------------
# GPU utilities – optional, fallback to CPU if unavailable.
# ---------------------------------------------------------------------
try:
    from gpu_utils import check_cuda_availability
    import torch
    CUDA_AVAILABLE = torch.cuda.is_available()
    if CUDA_AVAILABLE:
        print(f"Using CUDA GPU: {torch.cuda.get_device_name(0)}")
    else:
        print("CUDA not available, using CPU fallback")
except Exception as e:
    print(f"Warning: GPU utilities not available ({e}), using CPU only")
    CUDA_AVAILABLE = False

# ---------------------------------------------------------------------
# Optional MoviePy – required only for watermark removal.
# ---------------------------------------------------------------------
MOVIEPY_AVAILABLE = False
try:
    import moviepy.editor  # noqa: F401
    MOVIEPY_AVAILABLE = True
    print("[OK] MoviePy available for video processing")
except ImportError:
    print("[WARN] MoviePy not available - watermark removal will be disabled")

# ---------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------
def validate_file_exists(filepath, step_name):
    """Raise a clear error if a required intermediate file is missing."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"{step_name}: File not found: {filepath}")
    return True

def safe_copy_file(src, dst):
    try:
        shutil.copy2(src, dst)
        return True
    except Exception as e:
        print(f"Error copying {src} -> {dst}: {e}")
        return False

def load_attack_modules():
    """Import attack functions and return a dict mapping attack names to callables."""
    modules = {}

    # =================================================================
    # LEGACY ATTACKS (v2.x)
    # =================================================================

    # 1. Metadata stripping (CPU only)
    try:
        from compression_attacks.metadata_stripper import strip_metadata
        modules['metadata'] = strip_metadata
        print("[OK] Metadata stripping module loaded")
    except Exception as e:
        print(f"[WARN] Failed to load metadata stripping: {e}")
        modules['metadata'] = None

    # 2. Watermark removal (requires MoviePy)
    try:
        from watermark_attacks.watermark_remover import remove_watermark_regeneration
        modules['watermark'] = remove_watermark_regeneration
        print("[OK] Watermark removal module loaded")
    except Exception as e:
        print(f"[WARN] Failed to load watermark removal: {e}")
        modules['watermark'] = None

    # 3. Pixel perturbation (GPU‑accelerated if available)
    try:
        if CUDA_AVAILABLE:
            from pixel_attacks.adversarial_perturbations_gpu import adversarial_perturbations_gpu as pixel_func
        else:
            from pixel_attacks.adversarial_perturbations_optimized import add_adversarial_perturbations_optimized as pixel_func
        modules['pixel'] = pixel_func
        print("[OK] Pixel perturbation module loaded")
    except Exception as e:
        print(f"[WARN] Failed to load pixel perturbation: {e}")
        modules['pixel'] = None

    # 4. Temporal perturbation
    try:
        if CUDA_AVAILABLE:
            from temporal_attacks.temporal_perturbations_gpu import temporal_perturbations_gpu as temporal_func
        else:
            from temporal_attacks.temporal_perturbations_optimized import add_temporal_perturbations_optimized as temporal_func
        modules['temporal'] = temporal_func
        print("[OK] Temporal perturbation module loaded")
    except Exception as e:
        print(f"[WARN] Failed to load temporal perturbation: {e}")
        modules['temporal'] = None

    # 5. Attribution / trace evasion
    try:
        if CUDA_AVAILABLE:
            from attribution_evasion.trace_evader_gpu import trace_evader_gpu as trace_func
        else:
            from attribution_evasion.trace_evader_optimized import evade_traceevader_optimized as trace_func
        modules['trace'] = trace_func
        print("[OK] Attribution evasion module loaded")
    except Exception as e:
        print(f"[WARN] Failed to load attribution evasion: {e}")
        modules['trace'] = None

    # 6. Recompression (always available)
    try:
        from compression_attacks.recompression import single_pass_recompress
        modules['recompress'] = single_pass_recompress
        print("[OK] Recompression module loaded")
    except Exception as e:
        print(f"[WARN] Failed to load recompression: {e}")
        modules['recompress'] = None

    # =================================================================
    # NEW v3.0 ATTACKS - Scenario #1 (Replay/Pre-recorded Evasion)
    # Target: DSP-FWA + FTCN
    # =================================================================

    # 7. Micro-jitter injection (sub-pixel motion)
    try:
        from replay_evasion.micro_jitter import micro_jitter_injection
        modules['micro_jitter'] = micro_jitter_injection
        print("[OK] Micro-jitter injection module loaded (v3.0)")
    except Exception as e:
        print(f"[WARN] Failed to load micro-jitter: {e}")
        modules['micro_jitter'] = None

    # 8. Analog capture chain simulation
    try:
        from replay_evasion.analog_simulation import analog_capture_simulation
        modules['analog_sim'] = analog_capture_simulation
        print("[OK] Analog capture simulation module loaded (v3.0)")
    except Exception as e:
        print(f"[WARN] Failed to load analog simulation: {e}")
        modules['analog_sim'] = None

    # 9. Camera recapture simulation
    try:
        from replay_evasion.camera_recapture import camera_recapture_simulation
        modules['camera_recapture'] = camera_recapture_simulation
        print("[OK] Camera recapture simulation module loaded (v3.0)")
    except Exception as e:
        print(f"[WARN] Failed to load camera recapture: {e}")
        modules['camera_recapture'] = None

    # =================================================================
    # NEW v3.0 ATTACKS - Scenario #3 (Smoothing/Puppeteering Evasion)
    # Target: AltFreezing
    # =================================================================

    # 10. Realistic sensor noise
    try:
        from smoothing_evasion.sensor_noise import realistic_sensor_noise
        modules['sensor_noise'] = realistic_sensor_noise
        print("[OK] Realistic sensor noise module loaded (v3.0)")
    except Exception as e:
        print(f"[WARN] Failed to load sensor noise: {e}")
        modules['sensor_noise'] = None

    # 11. Motion energy modulation
    try:
        from smoothing_evasion.motion_modulation import motion_energy_modulation
        modules['motion_mod'] = motion_energy_modulation
        print("[OK] Motion energy modulation module loaded (v3.0)")
    except Exception as e:
        print(f"[WARN] Failed to load motion modulation: {e}")
        modules['motion_mod'] = None

    # 12. Frame-rate chaos / Variable FPS
    try:
        from smoothing_evasion.frame_chaos import frame_rate_chaos
        modules['frame_chaos'] = frame_rate_chaos
        print("[OK] Frame-rate chaos module loaded (v3.0)")
    except Exception as e:
        print(f"[WARN] Failed to load frame chaos: {e}")
        modules['frame_chaos'] = None

    return modules

# ---------------------------------------------------------------------
# Attacks that use the strength parameter
# ---------------------------------------------------------------------
ATTACKS_WITH_STRENGTH = {
    # Legacy
    'pixel', 'temporal', 'trace',
    # v3.0 - all new attacks use strength
    'micro_jitter', 'analog_sim', 'camera_recapture',
    'sensor_noise', 'motion_mod', 'frame_chaos'
}

def run_attack_step(func, inp, outp, name, base_strength, profile=None):
    """
    Execute a single attack step with validation and error handling.

    Args:
        func: Attack function to call
        inp: Input video path
        outp: Output video path
        name: Attack name
        base_strength: User-specified strength (0.0-1.0)
        profile: Generator-specific attack profile (optional)
    """
    try:
        print(f"\n--- Executing {name} attack ---")
        validate_file_exists(inp, f"{name} input")

        # Calculate final strength using profile if available
        if profile and name in ATTACKS_WITH_STRENGTH:
            final_strength = get_attack_strength(profile, name, base_strength)
            if final_strength == 0.0:
                print(f"[SKIP] {name} is disabled for {profile.generator_name} (profile strength = 0.0)")
                # Copy input to output unchanged
                shutil.copy2(inp, outp)
                print(f"[SUCCESS] {name} skipped (video passed through): {outp}")
                return True
            print(f"[INFO] Profile: {profile.generator_name}")
            if base_strength > 0:
                print(f"[INFO] Profile strength multiplier: {final_strength / base_strength:.2f}x")
            print(f"[INFO] Final strength: {final_strength:.3f} (base {base_strength} * profile)")
        else:
            final_strength = base_strength

        # Call with or without strength based on attack type
        if name in ATTACKS_WITH_STRENGTH:
            func(inp, outp, final_strength)
        else:
            func(inp, outp)

        validate_file_exists(outp, f"{name} output")
        print(f"[SUCCESS] {name} completed: {outp}")
        return True
    except Exception as e:
        print(f"[FAILED] {name} failed: {e}")
        import traceback
        traceback.print_exc()
        return False

# ---------------------------------------------------------------------
# Default output path helper
# ---------------------------------------------------------------------
def default_output_path(input_path, strength, attack_name="aa"):
    """Generate a default output filename."""
    p = Path(input_path)
    base_output = p.with_name(p.stem + f"_{attack_name}_{strength}" + p.suffix)

    if not base_output.exists():
        return str(base_output)

    counter = 1
    while True:
        numbered_output = p.with_name(p.stem + f"_{attack_name}{counter}_{strength}" + p.suffix)
        if not numbered_output.exists():
            return str(numbered_output)
        counter += 1

# ---------------------------------------------------------------------
# Attack pipeline definitions
# ---------------------------------------------------------------------
ATTACK_PIPELINES = {
    # Legacy pipelines
    'all': ['metadata', 'pixel', 'temporal', 'trace', 'watermark', 'recompress'],
    'prime': ['pixel', 'temporal', 'trace', 'recompress'],

    # v3.0 Scenario-specific pipelines
    'scenario1': [
        'micro_jitter',      # Sub-pixel motion to break FTCN temporal similarity
        'analog_sim',        # Analog chain artifacts to break DSP-FWA frequency analysis
        'camera_recapture',  # Camera sensor effects
        'recompress'         # Final recompression
    ],

    'scenario3': [
        'sensor_noise',      # Realistic camera noise patterns
        'motion_mod',        # Natural micro-movements
        'frame_chaos',       # Variable frame timing
        'recompress'         # Final recompression
    ],

    # Combined attack (all v3.0 modules)
    'v3_full': [
        'micro_jitter',
        'analog_sim',
        'camera_recapture',
        'sensor_noise',
        'motion_mod',
        'frame_chaos',
        'recompress'
    ],

    # Light version (minimal processing for subtle effect)
    'v3_light': [
        'micro_jitter',
        'sensor_noise',
        'motion_mod',
        'recompress'
    ],

    # Chunked alternating attack (special handling - not a regular pipeline)
    'chunked_alternating': '__SPECIAL__',
}

# All available single attacks
SINGLE_ATTACKS = [
    # Legacy
    'metadata', 'pixel', 'temporal', 'trace', 'watermark', 'recompress',
    # v3.0
    'micro_jitter', 'analog_sim', 'camera_recapture',
    'sensor_noise', 'motion_mod', 'frame_chaos'
]

# All available pipeline names
PIPELINE_NAMES = list(ATTACK_PIPELINES.keys())

# Combined choices for CLI
ALL_ATTACK_CHOICES = SINGLE_ATTACKS + PIPELINE_NAMES

# ---------------------------------------------------------------------
# TSV Parameter Logging
# ---------------------------------------------------------------------
def log_attack_parameters(
    input_path: str,
    output_path: str,
    attack: str,
    strength: float,
    generator: str,
    attack_sequence: list
):
    """
    Log attack parameters to a TSV file in the source video directory.

    Format: source_filename <TAB> output_filename <TAB> timestamp <TAB> attack <TAB>
            strength <TAB> generator <TAB> pipeline_attacks
    """
    try:
        # Get filenames without directory paths
        source_filename = os.path.basename(input_path)
        output_filename = os.path.basename(output_path)

        # Get source directory
        source_dir = os.path.dirname(os.path.abspath(input_path))

        # TSV log file name: <source_filename>.txt
        source_name_without_ext = os.path.splitext(source_filename)[0]
        log_file = os.path.join(source_dir, f"{source_name_without_ext}.txt")

        # Prepare data fields
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pipeline_attacks = " -> ".join(attack_sequence) if attack_sequence else attack
        generator_str = generator if generator else "none"

        # Check if file exists to determine if we need a header
        file_exists = os.path.exists(log_file)

        # Write to TSV (append mode)
        with open(log_file, 'a', encoding='utf-8') as f:
            # Write header if file is new
            if not file_exists:
                f.write("source_file\toutput_file\ttimestamp\tattack_type\tstrength\tgenerator\tpipeline\n")

            # Write data row
            f.write(f"{source_filename}\t{output_filename}\t{timestamp}\t{attack}\t{strength:.3f}\t{generator_str}\t{pipeline_attacks}\n")

        print(f"[LOG] Parameters logged to: {log_file}")

    except Exception as e:
        print(f"[WARN] Failed to log parameters: {e}")
        # Don't fail the entire operation if logging fails

# ---------------------------------------------------------------------
# Main CLI entry point
# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Adversarial Video Attack Toolkit v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run Scenario #1 for Bytedance Seedance video (minimal intervention)
  python main.py --input video.mp4 --attack scenario1 --generator seedance --strength 0.5

  # Run Scenario #3 for RunwayML video (aggressive compensation)
  python main.py --input video.mp4 --attack scenario3 --generator runway --strength 0.5

  # Run full v3.0 pipeline for Kling AI video
  python main.py --input video.mp4 --attack v3_full --generator kling --strength 0.3

  # Show detailed profile info for a generator
  python main.py --show-profile seedance

  # Generic mode (no generator specified)
  python main.py --input video.mp4 --attack scenario1 --strength 0.5

Available Attacks:
  Legacy: metadata, pixel, temporal, trace, watermark, recompress
  v3.0 Scenario #1: micro_jitter, analog_sim, camera_recapture
  v3.0 Scenario #3: sensor_noise, motion_mod, frame_chaos

Available Pipelines:
  all       - Full legacy pipeline
  prime     - Legacy pixel+temporal+trace+recompress
  scenario1 - Defeats DSP-FWA + FTCN (replay detection)
  scenario3 - Defeats AltFreezing (smoothing detection)
  v3_full   - All v3.0 attacks combined
  v3_light  - Minimal v3.0 attacks for subtle effect

Generator Profiles (v3.1):
  seedance  - Bytedance Seedance 1.5 Pro (minimal intervention)
  kling     - Kling AI 2.6 Pro (slight imperfections)
  runway    - RunwayML Gen-3 Alpha Turbo (aggressive compensation)
  generic   - Unknown generator (balanced approach)
        """
    )

    parser.add_argument("--input", required=False, help="Path to the input video file")
    parser.add_argument("--output", required=False, help="Path for the output video")
    parser.add_argument(
        "--attack",
        required=False,
        choices=ALL_ATTACK_CHOICES,
        help="Attack or pipeline to run"
    )
    parser.add_argument(
        "--strength",
        type=float,
        default=0.5,
        help="Base strength parameter (0.1-1.0, default: 0.5). Modified by generator profile."
    )
    parser.add_argument(
        "--generator",
        type=str,
        default=None,
        help="AI video generator used. Applies model-specific attack profile. Supported: " + ", ".join(list_generators()) + " (plus aliases)"
    )
    parser.add_argument(
        "--list-attacks",
        action="store_true",
        help="List all available attacks and pipelines"
    )
    parser.add_argument(
        "--show-profile",
        type=str,
        help="Show detailed information about a generator profile. Supported: " + ", ".join(list_generators())
    )

    args = parser.parse_args()

    # Handle --show-profile
    if hasattr(args, 'show_profile') and args.show_profile:
        profile = get_profile(args.show_profile)
        print_profile_info(profile)
        sys.exit(0)

    # Handle --list-attacks
    if args.list_attacks:
        print("\n=== Available Single Attacks ===")
        print("Legacy: metadata, pixel, temporal, trace, watermark, recompress")
        print("\nv3.0 Scenario #1 (DSP-FWA + FTCN):")
        print("  micro_jitter   - Sub-pixel motion injection")
        print("  analog_sim     - Analog capture chain simulation")
        print("  camera_recapture - Camera-in-camera effects")
        print("\nv3.0 Scenario #3 (AltFreezing):")
        print("  sensor_noise   - Realistic camera sensor noise")
        print("  motion_mod     - Motion energy modulation")
        print("  frame_chaos    - Variable frame rate / timing jitter")
        print("\n=== Available Pipelines ===")
        for name, attacks in ATTACK_PIPELINES.items():
            print(f"  {name}: {' -> '.join(attacks)}")
        print("\n=== Generator Profiles ===")
        print("Use --generator <name> to apply model-specific attack profiles:")
        for gen in list_generators():
            print(f"  {gen}")
        print("\nUse --show-profile <name> for detailed profile information")
        sys.exit(0)

    # Validate required arguments for attack execution
    if not args.input or not args.attack:
        parser.error("--input and --attack are required when running attacks")

    # Validate strength parameter
    if args.strength < 0.1 or args.strength > 1.0:
        parser.error(f"--strength must be between 0.1 and 1.0, got {args.strength}")

    input_path = os.path.abspath(args.input)
    if not os.path.isfile(input_path):
        print(f"Error: Input file does not exist: {input_path}")
        sys.exit(1)

    attack_label = args.attack
    output_path = os.path.abspath(args.output) if args.output else default_output_path(input_path, args.strength, attack_label)

    # Ensure output directory exists
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    # Load attack modules
    print("\n=== Loading Attack Modules ===")
    modules = load_attack_modules()

    # Check for special attack scenarios
    if args.attack == 'chunked_alternating':
        # Special handling for chunked alternating attack
        print(f"\n=== Running Special Attack: Chunked Alternating ===")
        print(f"Strategy: Random chunks (19-33 frames) with alternating scenario1/scenario3")

        try:
            from chunked_attacks.alternating_chunked import alternating_chunked_attack

            alternating_chunked_attack(
                input_path=input_path,
                output_path=output_path,
                strength=args.strength,
                generator=args.generator
            )

            # Log parameters
            log_attack_parameters(
                input_path=input_path,
                output_path=output_path,
                attack=args.attack,
                strength=args.strength,
                generator=args.generator,
                attack_sequence=['chunked_alternating']
            )

            print(f"\n{'='*60}")
            print(f"[SUCCESS] Chunked alternating attack completed!")
            print(f"Output saved to: {output_path}")
            print(f"{'='*60}")
            sys.exit(0)

        except Exception as e:
            print(f"\n[ERROR] Chunked alternating attack failed: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)

    # Determine attack sequence
    if args.attack in ATTACK_PIPELINES:
        attack_sequence = ATTACK_PIPELINES[args.attack]
        if attack_sequence == '__SPECIAL__':
            print(f"\nError: {args.attack} is a special attack but was not handled properly.")
            sys.exit(1)
        print(f"\n=== Running Pipeline: {args.attack} ===")
        print(f"Attacks: {' -> '.join(attack_sequence)}")
    else:
        attack_sequence = [args.attack]
        print(f"\n=== Running Single Attack: {args.attack} ===")

    # Verify requested attacks are available
    unavailable = []
    for atk in attack_sequence:
        if modules.get(atk) is None:
            unavailable.append(atk)

    if unavailable:
        print(f"\nError: The following attacks are not available: {', '.join(unavailable)}")
        print("This may be due to missing dependencies. Check the warnings above.")
        sys.exit(1)

    # Get generator profile if specified
    profile = None
    if args.generator:
        profile = get_profile(args.generator)
        print(f"\n{'='*60}")
        print(f"[PROFILE] Using {profile.generator_name} attack profile")
        print(f"{'='*60}")
        print(f"Strategy: {profile.description}")
        print(f"Base strength: {args.strength}")
        print(f"\nProfile will adjust attack intensities to match generator characteristics.")
        print(f"Some attacks may be disabled (0.0) to preserve native strengths.")

    # Execute the pipeline
    print(f"\n{'='*60}")
    print(f"[PIPELINE] Starting execution")
    print(f"{'='*60}")
    print(f"Input: {input_path}")
    print(f"Output: {output_path}")
    print(f"Base Strength: {args.strength}")
    if profile:
        print(f"Generator: {profile.generator_name}")

    current_input = input_path
    temp_files = []

    for idx, atk_name in enumerate(attack_sequence):
        is_last = (idx == len(attack_sequence) - 1)
        if is_last:
            out_path = output_path
        else:
            # Use mkstemp to avoid Windows file locking issues
            fd, out_path = tempfile.mkstemp(suffix=".mp4")
            os.close(fd)  # Close handle immediately to avoid locks

        success = run_attack_step(modules[atk_name], current_input, out_path, atk_name, args.strength, profile)

        if not success:
            print("\nAborting pipeline due to error.")
            for f in temp_files:
                try:
                    os.remove(f)
                except Exception:
                    pass
            sys.exit(1)

        if not is_last:
            temp_files.append(out_path)
        current_input = out_path

    # Cleanup temporary files
    for f in temp_files:
        try:
            os.remove(f)
        except Exception:
            pass

    print(f"\n{'='*60}")
    print(f"[SUCCESS] All attacks completed!")
    print(f"Output saved to: {output_path}")
    print(f"{'='*60}")

    # Log attack parameters to TSV file
    log_attack_parameters(
        input_path=input_path,
        output_path=output_path,
        attack=args.attack,
        strength=args.strength,
        generator=args.generator,
        attack_sequence=attack_sequence
    )

if __name__ == "__main__":
    main()
