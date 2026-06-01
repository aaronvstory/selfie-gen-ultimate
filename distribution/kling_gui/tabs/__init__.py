"""Tab package for the Portrait-to-Video Toolkit."""

import os
import sys

# Ensure top-level modules (vision_analyzer, selfie_generator, etc.) are
# importable from within tab background threads.
# In frozen (PyInstaller) builds, use the bundled directory; otherwise
# append the app root so it doesn't shadow stdlib/installed packages.
if getattr(sys, "frozen", False):
    _bundle_dir = getattr(sys, "_MEIPASS", None)
    if _bundle_dir and _bundle_dir not in sys.path:
        sys.path.insert(0, _bundle_dir)
else:
    _app_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    if _app_root not in sys.path:
        sys.path.append(_app_root)

from .face_crop_tab import FaceCropTab
from .prep_tab import PrepTab
from .selfie_tab import SelfieTab
from .expand_tab import ExpandTab
from .video_tab import VideoTab

__all__ = ["FaceCropTab", "PrepTab", "SelfieTab", "ExpandTab", "VideoTab"]
