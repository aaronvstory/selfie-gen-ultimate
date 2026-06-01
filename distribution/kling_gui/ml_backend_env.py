"""Shared ML backend environment bootstrap for TensorFlow/Keras compatibility."""

from __future__ import annotations

import os


def ensure_ml_backend_env() -> None:
    """Set deterministic TensorFlow/Keras compatibility environment."""
    os.environ["TF_USE_LEGACY_KERAS"] = "1"
    os.environ["KERAS_BACKEND"] = "tensorflow"
