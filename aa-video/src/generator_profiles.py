"""
Generator-Specific Attack Profiles for AI Video Models

This module provides attack parameter profiles tailored to specific AI video generators.
The key insight: counter-measures increase detectability unless perfectly matched to
the generator's native noise profile.

Strategy:
- Preserve generators' native strengths (temporal coherence, micro-motion, etc.)
- Only add complementary perturbations that fill gaps
- Avoid introducing detectable patterns

Based on research and detector feedback analysis (2026).
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class AttackProfile:
    """
    Attack parameters for a specific generator.

    Each parameter represents a strength multiplier (0.0-2.0) where:
    - 0.0 = disabled
    - 0.1-0.5 = minimal (preserve native quality)
    - 0.5-1.0 = moderate (standard)
    - 1.0-2.0 = aggressive (compensate for weaknesses)
    """
    # Scenario #1 attacks (DSP-FWA + FTCN evasion)
    micro_jitter_strength: float
    analog_sim_strength: float
    camera_recapture_strength: float

    # Scenario #3 attacks (AltFreezing evasion)
    sensor_noise_strength: float
    motion_mod_strength: float
    frame_chaos_strength: float

    # Metadata
    generator_name: str
    description: str
    native_strengths: list[str]
    native_weaknesses: list[str]


# =====================================================================
# Generator Profiles
# =====================================================================

SEEDANCE_15_PRO = AttackProfile(
    generator_name="Bytedance Seedance 1.5 Pro",
    description="High-quality diffusion-based generator with excellent native temporal coherence",

    # Native Strengths (backed by research):
    # - Excellent temporal coherence (MMDiT architecture)
    # - Natural micro-motion baked in at generation time
    # - Tight lip-sync with millisecond-level timing
    # - Clean textures, stable lighting physics
    # - Maintains identity/posture/scene coherence
    native_strengths=[
        "Excellent temporal coherence",
        "Natural micro-motion (non-deterministic)",
        "Jitter baked in at generation",
        "Tight audio-visual sync",
        "Clean motion for human gestures"
    ],

    # Native Weaknesses:
    # - High-speed action sequences show some temporal inconsistency
    # - Motion stability rated 7.8/10
    native_weaknesses=[
        "High-speed action temporal inconsistency",
        "Motion stability 7.8/10"
    ],

    # Strategy: MINIMAL intervention - preserve native excellence
    # Sample #1 (raw Seedance) passed as "real" - don't break what works!

    micro_jitter_strength=0.0,  # DISABLED - already has native jitter
    analog_sim_strength=0.2,     # MINIMAL - add subtle frequency irregularities only
    camera_recapture_strength=0.15,  # MINIMAL - add subtle sensor characteristics

    sensor_noise_strength=0.2,   # MINIMAL - add subtle complementary noise
    motion_mod_strength=0.0,     # DISABLED - already has excellent natural micro-motion
    frame_chaos_strength=0.0,    # DISABLED - already has excellent temporal coherence
)


KLING_AI_26_PRO = AttackProfile(
    generator_name="Kling AI 2.6 Pro",
    description="Production-grade generator with strong temporal coherence and minimal jitter",

    # Native Strengths:
    # - Better temporal coherence with grounded motion
    # - Smoother motion without typical "AI jitter"
    # - Precise hand movements without artifacts
    # - Natural facial expressions and lip-syncing
    # - Strong identity/body consistency across frames
    native_strengths=[
        "Strong temporal coherence",
        "Smooth motion without jitter",
        "Precise hand/face movements",
        "Natural lip-syncing",
        "Identity consistency"
    ],

    # Native Weaknesses:
    # - Motion can appear "too smooth" (AltFreezing risk)
    # - Some residual flicker/warping (though minimized)
    # - Compute-intensive (may introduce encoding artifacts)
    native_weaknesses=[
        "Motion sometimes too smooth",
        "Minimal flicker/warping",
        "Heavy encoding"
    ],

    # Strategy: Add slight imperfections to counter "too perfect" appearance

    micro_jitter_strength=0.15,  # MINIMAL - add tiny random motion to break smoothness
    analog_sim_strength=0.3,     # LOW - add frequency domain variety
    camera_recapture_strength=0.25,  # LOW - add subtle sensor imperfections

    sensor_noise_strength=0.3,   # LOW - add subtle realistic noise
    motion_mod_strength=0.2,     # MINIMAL - add slight natural imperfection
    frame_chaos_strength=0.15,   # MINIMAL - add tiny temporal variation
)


RUNWAY_GEN3_TURBO = AttackProfile(
    generator_name="RunwayML Gen-3 Alpha Turbo",
    description="Fast generator with Act-One facial performance, more artifacts than competitors",

    # Native Strengths:
    # - 7x faster generation
    # - Facial performance capture (Act-One)
    # - Cost-effective
    native_strengths=[
        "Fast generation (7x speedup)",
        "Facial performance transfer",
        "Cost-effective"
    ],

    # Native Weaknesses (significant):
    # - Lip-sync feels off
    # - Minor flickering and motion inconsistency
    # - AI artifacts in certain scenes
    # - Prone to distortions with dynamic movement
    # - Low-light/reflective surface issues
    # - Capped at 720p resolution
    # - Only captures facial expressions (not body/background)
    native_weaknesses=[
        "Lip-sync imperfections",
        "Flickering and motion inconsistency",
        "Visible AI artifacts",
        "Distortions in dynamic scenes",
        "Low-light quality issues",
        "720p resolution limit"
    ],

    # Strategy: AGGRESSIVE intervention - compensate for native weaknesses
    # Need to add missing realism and mask artifacts

    micro_jitter_strength=0.6,   # MODERATE - mask flickering with intentional jitter
    analog_sim_strength=0.5,     # MODERATE - add frequency domain noise to mask artifacts
    camera_recapture_strength=0.5,  # MODERATE - add sensor characteristics to mask AI look

    sensor_noise_strength=0.6,   # MODERATE - add realistic noise to mask artifacts
    motion_mod_strength=0.7,     # MODERATE-HIGH - add natural micro-movements (missing in native)
    frame_chaos_strength=0.6,    # MODERATE - add temporal variation to mask inconsistency
)


# Default profile for unknown/generic generators
GENERIC_PROFILE = AttackProfile(
    generator_name="Generic / Unknown",
    description="Balanced profile for unknown generators - moderate intervention",

    native_strengths=["Unknown"],
    native_weaknesses=["Unknown"],

    # Balanced approach - assume nothing
    micro_jitter_strength=0.5,
    analog_sim_strength=0.5,
    camera_recapture_strength=0.5,
    sensor_noise_strength=0.5,
    motion_mod_strength=0.5,
    frame_chaos_strength=0.5,
)


# =====================================================================
# Profile Registry
# =====================================================================

GENERATOR_PROFILES: Dict[str, AttackProfile] = {
    # Primary names
    "seedance": SEEDANCE_15_PRO,
    "seedance-1.5-pro": SEEDANCE_15_PRO,
    "bytedance": SEEDANCE_15_PRO,

    "kling": KLING_AI_26_PRO,
    "kling-2.6-pro": KLING_AI_26_PRO,
    "kling-ai": KLING_AI_26_PRO,

    "runway": RUNWAY_GEN3_TURBO,
    "runway-gen3": RUNWAY_GEN3_TURBO,
    "runway-gen3-turbo": RUNWAY_GEN3_TURBO,
    "gen3": RUNWAY_GEN3_TURBO,
    "act-one": RUNWAY_GEN3_TURBO,

    "generic": GENERIC_PROFILE,
    "unknown": GENERIC_PROFILE,
}


def get_profile(generator_name: Optional[str]) -> AttackProfile:
    """
    Get attack profile for a specific generator.

    Args:
        generator_name: Name of the generator (case-insensitive)
                       Options: seedance, kling, runway, generic

    Returns:
        AttackProfile with generator-specific parameters
    """
    if generator_name is None:
        return GENERIC_PROFILE

    key = generator_name.lower().strip()
    return GENERATOR_PROFILES.get(key, GENERIC_PROFILE)


def list_generators() -> list[str]:
    """List all supported generator names (primary keys only)."""
    return ["seedance", "kling", "runway", "generic"]


def list_all_generator_aliases() -> list[str]:
    """List all generator names including aliases."""
    return sorted(GENERATOR_PROFILES.keys())


def get_attack_strength(profile: AttackProfile, attack_name: str, user_strength: float) -> float:
    """
    Calculate final attack strength based on profile and user input.

    Args:
        profile: Generator-specific attack profile
        attack_name: Name of attack module
        user_strength: User-specified strength multiplier (0.0-1.0)

    Returns:
        Final strength value (profile_strength * user_strength)
    """
    attack_map = {
        'micro_jitter': profile.micro_jitter_strength,
        'analog_sim': profile.analog_sim_strength,
        'camera_recapture': profile.camera_recapture_strength,
        'sensor_noise': profile.sensor_noise_strength,
        'motion_mod': profile.motion_mod_strength,
        'frame_chaos': profile.frame_chaos_strength,
    }

    profile_strength = attack_map.get(attack_name, 1.0)

    # If profile strength is 0, attack is disabled
    if profile_strength == 0.0:
        return 0.0

    # Multiply profile strength by user strength
    return profile_strength * user_strength


def print_profile_info(profile: AttackProfile):
    """Print detailed information about a generator profile."""
    print(f"\n=== {profile.generator_name} ===")
    print(f"\n{profile.description}\n")

    print("Native Strengths:")
    for strength in profile.native_strengths:
        print(f"  + {strength}")

    print("\nNative Weaknesses:")
    for weakness in profile.native_weaknesses:
        print(f"  - {weakness}")

    print("\nAttack Parameters:")
    print(f"  Scenario #1 (Replay Evasion):")
    print(f"    micro_jitter:      {profile.micro_jitter_strength:.2f}")
    print(f"    analog_sim:        {profile.analog_sim_strength:.2f}")
    print(f"    camera_recapture:  {profile.camera_recapture_strength:.2f}")

    print(f"  Scenario #3 (Smoothing Evasion):")
    print(f"    sensor_noise:      {profile.sensor_noise_strength:.2f}")
    print(f"    motion_mod:        {profile.motion_mod_strength:.2f}")
    print(f"    frame_chaos:       {profile.frame_chaos_strength:.2f}")

    print()


if __name__ == '__main__':
    # Test/demo
    print("Available Generator Profiles:\n")

    for gen in list_generators():
        profile = get_profile(gen)
        print_profile_info(profile)
