"""Back-compat shim: the FFmpeg loop implementation moved to
``automation/video_loop.py`` (2026-06-11) so the headless automation
pipeline can loop videos without importing the tkinter-laden ``kling_gui``
package. GUI call sites (queue_manager, config_panel, ``kling_gui.__init__``)
keep importing from here unchanged.

Tests that patch GUI loop behavior should patch THIS module's names
(``kling_gui.video_looper.create_looped_video``) — the GUI imports resolve
through this namespace at call time.
"""

from automation.video_loop import (  # noqa: F401
    _summarize_ffmpeg_error,
    check_ffmpeg_available,
    create_looped_video,
    get_video_duration,
)

__all__ = [
    "check_ffmpeg_available",
    "create_looped_video",
    "get_video_duration",
]
