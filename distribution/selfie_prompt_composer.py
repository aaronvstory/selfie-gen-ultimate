"""Compose selfie prompts from configurable data arrays."""

from typing import Optional, List

DEFAULT_GENDER = "female"
DEFAULT_CAMERA_STYLE = "phone_selfie"


# Camera/pose styles
CAMERA_STYLES = {
    "phone_selfie": (
        "casual phone selfie, slightly above eye level, one arm extended holding camera"
    ),
    "mirror_selfie": (
        "mirror selfie, phone visible in reflection, natural bathroom/bedroom lighting"
    ),
    "professional": (
        "professional headshot, studio lighting, neutral background, shoulders visible"
    ),
    "candid": (
        "candid portrait, natural pose, looking slightly off-camera, outdoor setting"
    ),
    "close_up": (
        "extreme close-up portrait, face filling frame, shallow depth of field"
    ),
}

# Background options
BACKGROUNDS = {
    "auto": None,  # Let the model decide
    "indoor_casual": "cozy indoor setting, warm ambient lighting",
    "outdoor_natural": "outdoor setting with natural greenery and soft sunlight",
    "urban": "urban cityscape background, modern architecture",
    "studio": "clean studio backdrop, professional lighting",
    "cafe": "cafe or coffee shop setting, warm tones, bokeh background",
}

# Lighting styles
LIGHTING_STYLES = {
    "auto": None,
    "natural": "soft natural lighting",
    "golden_hour": "warm golden hour sunlight, soft shadows",
    "studio_soft": "soft studio lighting, minimal shadows",
    "dramatic": "dramatic side lighting, strong shadows",
    "ring_light": "ring light illumination, even face lighting, slight catchlights in eyes",
}

# Gender-specific base descriptors
GENDER_DESCRIPTORS = {
    "female": "a young woman",
    "male": "a young man",
    "neutral": "a person",
}


class SelfiePromptComposer:
    """Compose prompts for selfie generation from configurable options."""

    def compose(
        self,
        gender: str = DEFAULT_GENDER,
        camera_style: str = DEFAULT_CAMERA_STYLE,
        background: Optional[str] = None,
        lighting: Optional[str] = None,
        additional_details: str = "",
    ) -> str:
        """Compose a selfie prompt from component parts.

        Args:
            gender: "female", "male", or "neutral"
            camera_style: Key from CAMERA_STYLES
            background: Key from BACKGROUNDS, or None for auto
            lighting: Key from LIGHTING_STYLES, or None for auto
            additional_details: Extra prompt text to append

        Returns:
            Composed prompt string.
        """
        parts = []

        # Subject
        subject = GENDER_DESCRIPTORS.get(gender, GENDER_DESCRIPTORS["neutral"])
        parts.append(f"Photo of {subject}")

        # Camera style
        style = CAMERA_STYLES.get(camera_style, CAMERA_STYLES["phone_selfie"])
        parts.append(style)

        # Background
        if background and background != "auto":
            bg = BACKGROUNDS.get(background)
            if bg:
                parts.append(bg)

        # Lighting
        if lighting and lighting != "auto":
            light = LIGHTING_STYLES.get(lighting)
            if light:
                parts.append(light)

        # Quality suffix
        parts.append(
            "photorealistic, high quality, detailed skin texture, natural expression"
        )

        # Additional details
        if additional_details.strip():
            parts.append(additional_details.strip())

        return ", ".join(parts)

    @staticmethod
    def get_camera_styles() -> dict:
        """Return available camera styles."""
        return dict(CAMERA_STYLES)

    @staticmethod
    def get_backgrounds() -> dict:
        """Return available backgrounds."""
        return dict(BACKGROUNDS)

    @staticmethod
    def get_lighting_styles() -> dict:
        """Return available lighting styles."""
        return dict(LIGHTING_STYLES)

    @staticmethod
    def get_gender_options() -> List[str]:
        """Return available gender options."""
        return list(GENDER_DESCRIPTORS.keys())
