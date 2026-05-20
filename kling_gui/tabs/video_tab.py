"""Video Tab — Wraps existing ConfigPanel and Queue for video generation."""

import os
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from ..theme import COLORS, FONT_FAMILY, TTK_BTN_SUCCESS, TTK_BTN_WORKFLOW, debounce_command
from ..image_state import ImageSession


class VideoTab(tk.Frame):
    """Tab 4: Video generation settings (wraps existing widgets)."""

    def __init__(
        self,
        parent,
        image_session: ImageSession,
        log_callback: Callable[[str, str], None],
        on_files_dropped: Optional[Callable[[List[str]], None]] = None,
        **kwargs,
    ):
        """
        Args:
            parent: Tab parent widget (the Notebook)
            image_session: Shared carousel state
            log_callback: log(message, level)
            on_files_dropped: Callback to add files directly to the queue
        """
        super().__init__(parent, bg=COLORS["bg_panel"], **kwargs)
        self.image_session = image_session
        self.log = log_callback
        self.config_panel = None
        self._on_files_dropped_cb = on_files_dropped

        # "Use Carousel Image" button at top
        use_carousel_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        use_carousel_frame.pack(fill=tk.X, padx=10, pady=(5, 2))

        # Workflow-primary on Step 3 — main "start" action for the tab.
        self.use_carousel_btn = ttk.Button(
            use_carousel_frame,
            text="Start - Using Carousel Image",
            style=TTK_BTN_WORKFLOW,
            command=debounce_command(self._on_use_carousel, key="video_use_carousel"),
        )
        self.use_carousel_btn.pack(side=tk.LEFT)

        tk.Label(
            use_carousel_frame,
            text="Use active carousel image for video generation",
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=COLORS["text_dim"],
        ).pack(side=tk.LEFT, padx=8)

    def attach_config_panel(self, config_panel):
        """Attach the ConfigPanel into this tab."""
        self.config_panel = config_panel
        config_panel.pack(fill=tk.X, padx=0, pady=(0, 3))

    def _on_use_carousel(self):
        """Add the active carousel image to the video processing flow."""
        path = self.image_session.active_image_path
        if not path:
            self.log("No image in carousel to use", "warning")
            return
        basename = os.path.basename(path)
        if self._on_files_dropped_cb:
            self._on_files_dropped_cb([path])
            self.log(
                f"Added carousel image for video generation: {basename}", "info"
            )
            return
        self.log(
            "Could not add image for generation — no handler available", "warning"
        )
