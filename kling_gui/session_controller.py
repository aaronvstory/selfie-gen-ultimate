"""Session save/autosave controller for the main window."""

from __future__ import annotations

import os
import tkinter as tk
from typing import Callable, Optional

from . import session_manager


class SessionController:
    """Encapsulate manual save and autosave orchestration."""

    def __init__(
        self,
        root: tk.Tk,
        data_dir: str,
        image_session,
        config_getter: Callable[[], dict],
        log_callback: Callable[[str, str], None],
        logger=None,
        autosave_debounce_ms: int = 1200,
    ):
        self.root = root
        self.data_dir = data_dir
        self.image_session = image_session
        self._config_getter = config_getter
        self._log = log_callback
        self._logger = logger
        self.autosave_debounce_ms = autosave_debounce_ms
        self.autosave_job = None
        self.autosave_suspended = False

    @property
    def config(self) -> dict:
        return self._config_getter()

    def save_current_session_snapshot(self) -> bool:
        """Save a manual snapshot of current session."""
        if not self.image_session.count:
            return True
        try:
            path = session_manager.save_session(
                self.data_dir,
                self.image_session,
                self.config,
                session_kind=session_manager.SESSION_KIND_MANUAL,
            )
            self._log(f"Session saved: {os.path.basename(path)}", "success")
            return True
        except Exception as exc:
            self._log(f"Save failed: {exc}", "error")
            return False

    def start_autosave_timer(self):
        if not self.config.get("session_autosave_enabled", True):
            return
        interval = self.config.get("session_autosave_interval", "after_api_action")
        if interval == "after_api_action":
            return
        ms_map = {"5min": 300000, "10min": 600000, "15min": 900000}
        ms = ms_map.get(interval, 300000)
        try:
            if self.root.winfo_exists():
                self.root.after(ms, self.autosave_tick)
        except tk.TclError:
            return

    def autosave_tick(self):
        if self.image_session.count > 0:
            self.queue_autosave(reason="timer_tick", debounce_ms=350)
        self.start_autosave_timer()

    def on_image_session_changed(self):
        if self.autosave_suspended:
            return
        self.queue_autosave(reason="state_change", debounce_ms=self.autosave_debounce_ms)

    def queue_autosave(self, reason: str = "state_change", debounce_ms: Optional[int] = None):
        if not self.config.get("session_autosave_enabled", True):
            return
        if self.image_session.count == 0:
            return
        if not self.root.winfo_exists():
            return
        delay = self.autosave_debounce_ms if debounce_ms is None else max(0, int(debounce_ms))
        if self.autosave_job is not None:
            try:
                self.root.after_cancel(self.autosave_job)
            except Exception:
                pass
        self.autosave_job = self.root.after(delay, lambda: self.run_debounced_autosave(reason))

    def run_debounced_autosave(self, reason: str):
        self.autosave_job = None
        self.maybe_autosave(reason=reason)

    def maybe_autosave(self, reason: str = "manual"):
        if not self.config.get("session_autosave_enabled", True):
            return
        if self.image_session.count == 0:
            return
        try:
            session_manager.save_session(
                self.data_dir,
                self.image_session,
                self.config,
                session_kind=session_manager.SESSION_KIND_AUTOSAVE,
                project_key=session_manager.get_project_key(self.image_session),
                autosave_retention=int(self.config.get("session_autosave_retention", 10)),
                # One rolling file per project; the write is skipped entirely
                # when content is unchanged (idle ticks / debounced no-ops).
                skip_if_unchanged=True,
            )
        except Exception as exc:
            if self._logger:
                self._logger.warning("Auto-save failed (%s): %s", reason, exc)

    def flush_before_close(self):
        if self.autosave_job is not None:
            try:
                self.root.after_cancel(self.autosave_job)
            except Exception:
                pass
            self.autosave_job = None
        if self.image_session.count > 0:
            self.maybe_autosave(reason="close")
