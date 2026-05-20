"""Video Inspector — in-app preview and A/B comparison modal.

Two layers:

* ``VideoFrame`` — a single-video preview widget backed by an OpenCV
  ``VideoCapture`` decoded on a daemon thread, with frames pushed back
  to the Tk thread via ``self.after(0, ...)``. No audio. Graceful
  degradation when OpenCV is missing or the file can't be opened.

* ``VideoInspectorModal`` — a ``tk.Toplevel`` that owns two
  ``VideoFrame`` slots, a listbox of discovered videos, a metadata
  panel, transport controls, and a master timer driving frame
  advancement. ``transient`` + ``grab_set`` (modal scope, NOT global)
  + ``focus_set`` — mirrors ``SessionManagerDialog`` so macOS behaves.

* ``open_video_inspector`` — singleton factory. If an existing modal
  is alive, focuses it instead of constructing a second one (Toplevel
  + thread leak prevention).

Pipeline-rule reminders embedded throughout:

* macOS Tk is sensitive to off-thread widget mutation — ALL canvas
  mutations and ``ImageTk.PhotoImage`` construction happen via
  ``self.after(0, ...)`` callbacks on the main Tk thread.

* OpenCV's macOS-ARM wheels sometimes lag — ``cv2`` is imported lazily
  inside ``load()`` so a missing wheel falls back to "Open Externally"
  instead of crashing the whole GUI.

* PhotoImage objects are GC'd if no strong reference is held —
  ``self._photo`` is an instance attribute, mirroring carousel_widget.py
  line 123. ``master=self._canvas`` is passed for consistency with the
  carousel's ImageTk usage.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .theme import (
    BUTTON_DISABLED_TEXT_COLOR,
    BUTTON_TEXT_COLOR,
    COLORS,
    FONT_FAMILY,
    apply_macos_button_fix,
    debounce_command,
)
from .video_discovery import VideoGroup, find_video_groups
from .video_metadata import VideoMetadata, parse_video_filename

logger = logging.getLogger(__name__)


# Master timer tick interval (~25 fps) and decoded display size. We
# decode AT display size (cv2 resize during read) so we don't waste
# cycles on full-resolution frames we'll only shrink anyway.
_TICK_MS = 40
_DISPLAY_W = 480
_DISPLAY_H = 270


def _open_externally(path: Path) -> None:
    """Open a file in the OS default app. Cross-platform via
    ``webbrowser.open`` with a ``file://`` URI fallback for paths
    containing characters that confuse the OS file handler."""
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception:
        try:
            webbrowser.open(str(path.resolve()))
        except Exception:
            logger.exception("Failed to open externally: %s", path)


# ──────────────────────────────────────────────────────────────────────
# VideoFrame
# ──────────────────────────────────────────────────────────────────────


class VideoFrame(tk.Frame):
    """A single video preview slot. cv2 frame loop, no audio.

    Public lifecycle:
        load(path)        # returns True on success
        clear()           # release capture, stop decoder thread
        step_to(frame)    # external seek (used by the modal master timer)
        destroy()         # override: clear() then super().destroy()

    Threading model:
        Each load() spawns ONE daemon decoder thread, fed via a
        queue.Queue(maxsize=1) carrying the next frame index to decode.
        The thread pulls a frame, builds a PIL.Image, posts back via
        self.after(0, self._render_pil_image, img). PhotoImage is
        constructed in _render_pil_image — ON the Tk thread — never in
        the worker. clear() sets _stop_event; the worker checks it at
        every iteration top and exits cleanly without a .join() call
        (daemon thread; no UI block).
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str = "",
        log_callback: Optional[Callable[[str, str], None]] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, **kwargs)
        self.configure(bg=COLORS["bg_main"])

        self._title = title
        self._log_callback = log_callback
        self._video_path: Optional[Path] = None
        self._cv2_cap = None  # cv2.VideoCapture | None
        self._cv2 = None  # cached cv2 module reference (lazy import)
        self._frame_count = 0
        self._fps = 25.0
        self._current_frame = -1
        self._photo: Optional[tk.PhotoImage] = None  # GC anchor
        self._overlay_drawer: Optional[Callable] = None  # dormant V1
        # Live canvas dimensions tracked on <Configure> so the
        # decoder thread can resize frames to the actual visible area
        # (with aspect-preserving fit). Defaults are the _DISPLAY_W/H
        # constants until the first Configure event lands.
        self._canvas_w: int = _DISPLAY_W
        self._canvas_h: int = _DISPLAY_H

        # Decoder-thread lifecycle: generation-id locking. Each load()
        # increments _generation_id and the new decoder thread receives
        # its OWN stop_event, cap, cv2 reference, and request queue —
        # all local to that generation. An old decoder still draining
        # the previous load's queue cannot touch the new capture and
        # _render_pil_image discards stale-generation frames before
        # touching any widget state. Without this guard, a slow-exit
        # old worker could observe self._stop_event after load()
        # replaced it with a fresh unset Event (and continue rendering
        # against the new VideoCapture). See GPT-5.5 PR #43 feedback.
        self._generation_id: int = 0
        self._stop_event = threading.Event()
        self._cap_lock = threading.Lock()
        self._frame_request: "queue.Queue[int]" = queue.Queue(maxsize=1)
        self._decoder_thread: Optional[threading.Thread] = None

        # Title row
        if title:
            self._title_lbl = tk.Label(
                self,
                text=title,
                bg=COLORS["bg_main"],
                fg=COLORS["text_light"],
                font=(FONT_FAMILY, 9, "bold"),
            )
            self._title_lbl.pack(side=tk.TOP, anchor=tk.W, pady=(0, 2), padx=4)

        # Frame canvas
        self._canvas = tk.Canvas(
            self,
            width=_DISPLAY_W,
            height=_DISPLAY_H,
            bg="#000000",
            highlightthickness=0,
        )
        self._canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._placeholder_id = self._canvas.create_text(
            _DISPLAY_W // 2,
            _DISPLAY_H // 2,
            text="(no video)",
            fill=COLORS["text_dim"],
            font=(FONT_FAMILY, 11),
        )
        # Re-center the placeholder text + any rendered frame when the
        # canvas resizes. Items created with absolute coords (240, 135
        # from _DISPLAY_W/H constants) would otherwise stay anchored at
        # the original top-left even after pack/grid expanded the
        # canvas to e.g. 700×400 — text appears off-center toward the
        # upper-left of the actual canvas area.
        self._canvas.bind("<Configure>", self._on_canvas_resize, add="+")

        # Per-slot "Open Externally" button — shown only after a failed
        # load or when the user wants the OS player. Always created so
        # tests can find it; placed hidden until needed.
        self._open_external_btn = tk.Button(
            self,
            text="Open Externally",
            command=self._on_open_externally,
            font=(FONT_FAMILY, 8),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            relief=tk.FLAT,
            bd=0,
            padx=8,
            pady=2,
            cursor="hand2",
            state=tk.DISABLED,
        )
        apply_macos_button_fix(self._open_external_btn)
        self._open_external_btn.pack(side=tk.TOP, pady=(2, 0))

    # ── Public API ──────────────────────────────────────────────────

    def is_loaded(self) -> bool:
        return self._cv2_cap is not None

    def get_frame_count(self) -> int:
        return self._frame_count

    def get_fps(self) -> float:
        return self._fps

    def get_current_frame(self) -> int:
        return self._current_frame

    def set_overlay_drawer(self, fn) -> None:
        """V1 stores but never invokes a non-None drawer — kept as
        future-extension seam for ROI/diff/metric overlays."""
        self._overlay_drawer = fn

    def load(self, video_path: Path) -> bool:
        """Open ``video_path`` via cv2. Returns True on success.

        On any failure (missing cv2, cap.isOpened() False, exception)
        the canvas shows an error label + "Open Externally" button is
        enabled, and False is returned. The caller can still rely on
        ``is_loaded() == False``."""
        self.clear()  # idempotent — also resets state
        self._video_path = Path(video_path)

        # Lazy cv2 import — keeps the GUI healthy even on machines
        # where opencv-python isn't installed.
        try:
            import cv2  # type: ignore
        except ImportError as exc:
            self._show_error(f"OpenCV not installed ({exc})")
            return False
        self._cv2 = cv2

        try:
            cap = cv2.VideoCapture(str(self._video_path))
        except Exception as exc:
            self._show_error(f"Failed to open ({type(exc).__name__})")
            return False

        if not cap.isOpened():
            try:
                cap.release()
            except Exception:
                pass
            self._show_error("Cannot decode this video")
            return False

        self._cv2_cap = cap
        self._frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        raw_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        # Clamp FPS to a sane range; cv2 returns 0 for some containers.
        self._fps = raw_fps if 1.0 <= raw_fps <= 120.0 else 25.0
        self._current_frame = -1

        # Enable "Open Externally" now that we have a real path on hand.
        try:
            self._open_external_btn.config(state=tk.NORMAL)
        except tk.TclError:
            pass

        # New generation: fresh stop_event + request queue, all passed
        # as ARGS into the decoder so it observes its own state — not
        # whatever self.* points at by the time it ticks. An old
        # worker that hasn't exited yet keeps its own (now-set) event.
        self._generation_id += 1
        gen_id = self._generation_id
        stop_event = threading.Event()
        request_queue: "queue.Queue[int]" = queue.Queue(maxsize=1)
        self._stop_event = stop_event
        self._frame_request = request_queue

        self._decoder_thread = threading.Thread(
            target=self._decoder_loop,
            args=(gen_id, stop_event, request_queue, cap, cv2),
            daemon=True,
            name=f"VideoFrame-decoder-{gen_id}",
        )
        self._decoder_thread.start()
        self.step_to(0)
        return True

    def step_to(self, frame_index: int) -> None:
        """Request the decoder thread render frame ``frame_index``.

        Non-blocking. Drop-oldest semantics — if the worker hasn't
        consumed the prior request yet, replace it (we always want the
        newest frame target, never to play catch-up)."""
        if self._cv2_cap is None:
            return
        if frame_index < 0:
            frame_index = 0
        if self._frame_count > 0 and frame_index >= self._frame_count:
            frame_index = self._frame_count - 1
        # Drain prior request (non-blocking) then push new one.
        try:
            while True:
                self._frame_request.get_nowait()
        except queue.Empty:
            pass
        try:
            self._frame_request.put_nowait(frame_index)
        except queue.Full:
            pass

    def clear(self) -> None:
        """Stop the decoder thread, release the capture, blank the canvas.

        Bumps the generation so any after-callback the (now-doomed)
        worker has already posted to the Tk queue gets rejected by
        _render_pil_image's generation check before touching widgets.
        """
        # Invalidate any in-flight render callbacks from the current
        # generation. Anything the worker posts after this point will
        # see a mismatched generation_id and abort cleanly.
        self._generation_id += 1
        self._stop_event.set()
        # Unblock the decoder if it's waiting on the request queue.
        try:
            self._frame_request.put_nowait(-1)
        except queue.Full:
            pass

        with self._cap_lock:
            cap = self._cv2_cap
            self._cv2_cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

        self._photo = None
        self._video_path = None
        self._frame_count = 0
        self._current_frame = -1
        try:
            self._canvas.delete("all")
            self._placeholder_id = self._canvas.create_text(
                _DISPLAY_W // 2,
                _DISPLAY_H // 2,
                text="(no video)",
                fill=COLORS["text_dim"],
                font=(FONT_FAMILY, 11),
            )
        except tk.TclError:
            pass
        try:
            self._open_external_btn.config(state=tk.DISABLED)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        try:
            self.clear()
        except Exception:
            pass
        try:
            super().destroy()
        except Exception:
            pass

    # ── Internal: decoder thread ────────────────────────────────────

    def _decoder_loop(
        self,
        generation_id: int,
        stop_event: threading.Event,
        request_queue: "queue.Queue[int]",
        cap,
        cv2_mod,
    ) -> None:
        """Worker thread. Pulls frame-index requests; decodes; posts
        the PIL.Image back to the Tk thread via self.after(0, ...).

        All state passed by VALUE (not via self.*) so an old worker
        that hasn't exited yet operates on its own generation's event,
        queue, and capture — never the new ones. The generation_id is
        threaded through so _render_pil_image can drop stale frames.

        Never builds an ImageTk.PhotoImage here (Tk-thread only).
        Never touches a Tk widget here.
        """
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            self.after(0, lambda: self._show_error("PIL not installed"))
            return

        while not stop_event.is_set():
            try:
                frame_index = request_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if stop_event.is_set():
                return
            if frame_index < 0:
                # Sentinel from clear(); exit.
                return

            # Cap is LOCAL to this generation — passed in as an arg, not
            # read from self.* — so an old worker can't race the new
            # capture. The lock that used to wrap this block has been
            # removed (code-reviewer P2, PR #43): the previous
            # implementation held self._cap_lock across the blocking
            # cap.read() which on an H.264 keyframe decode can take
            # 100-200ms, stalling the Tk thread's clear() call for the
            # full decode window. Since cap is never shared, the lock
            # served no real purpose.
            #
            # Sequential-read fast path: cap.set(CAP_PROP_POS_FRAMES) on
            # H.264/H.265 is O(N) from the nearest keyframe (Gemini #4
            # finding on PR #43). When the request is just "next frame",
            # skip the seek entirely — cv2 already advances by one on
            # cap.read(). Only seek for non-sequential jumps (scrub,
            # restart, A/B sync). The threshold (>1 frame delta)
            # tolerates the off-by-one from cap.get(POS_FRAMES) returning
            # the NEXT frame index after a successful read.
            try:
                current = int(cap.get(cv2_mod.CAP_PROP_POS_FRAMES) or 0)
                if abs(current - frame_index) > 1:
                    cap.set(cv2_mod.CAP_PROP_POS_FRAMES, frame_index)
                ok, frame_bgr = cap.read()
            except Exception:
                ok, frame_bgr = False, None

            if not ok or frame_bgr is None:
                continue

            try:
                # Aspect-preserving fit (PR #43 user feedback: video
                # was getting squashed/stretched because the previous
                # implementation blindly resized to _DISPLAY_W ×
                # _DISPLAY_H = 480×270 = 16:9). Real videos can be
                # 9:16 (portrait), 3:4 (selfie), or anything else.
                # Now we read the canvas's CURRENT size on the Tk
                # thread (canvas_w/h captured at frame_dim_call time)
                # and fit the source frame INSIDE that box preserving
                # aspect ratio. cv2 handles the actual resize.
                src_h, src_w = frame_bgr.shape[:2]
                # Pull the canvas dimensions captured by the last
                # Configure event (falls back to _DISPLAY_W/H before
                # the canvas has ever been laid out).
                target_w = self._canvas_w or _DISPLAY_W
                target_h = self._canvas_h or _DISPLAY_H
                # Fit-inside: scale by the smaller of the two ratios
                # so the entire source frame is visible (no crop).
                scale = min(target_w / src_w, target_h / src_h)
                new_w = max(1, int(src_w * scale))
                new_h = max(1, int(src_h * scale))
                resized = cv2_mod.resize(frame_bgr, (new_w, new_h))
                rgb = cv2_mod.cvtColor(resized, cv2_mod.COLOR_BGR2RGB)
                pil_img = Image.fromarray(rgb)
            except Exception:
                logger.exception("VideoFrame decode failed")
                continue

            # Hop back to the Tk thread for the actual widget update.
            # generation_id flows through so the renderer drops frames
            # from a superseded worker.
            self.after(
                0, self._render_pil_image, pil_img, frame_index, generation_id,
            )

    # ── Internal: Tk-thread render ──────────────────────────────────

    def _render_pil_image(
        self, pil_img, frame_index: int, generation_id: int = 0,
    ) -> None:
        """Construct PhotoImage + draw on canvas. RUNS ON TK THREAD.

        Stale-generation guard: if an old decoder worker posted this
        frame *after* a newer load() has already started, the
        generation_ids won't match and we drop the render. This is
        what prevents a slow-exiting old worker from clobbering the
        canvas with a frame from the previous video.
        """
        if generation_id != self._generation_id:
            return
        # Guard for the case where the widget went away mid-decode.
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

        try:
            from PIL import ImageTk  # type: ignore
        except ImportError:
            self._show_error("PIL.ImageTk not installed")
            return

        try:
            photo = ImageTk.PhotoImage(pil_img, master=self._canvas)
        except Exception:
            logger.exception("VideoFrame PhotoImage construction failed")
            return

        self._photo = photo  # GC anchor (mirrors carousel_widget:123)
        self._current_frame = frame_index
        try:
            self._canvas.delete("all")
            self._canvas.create_image(
                _DISPLAY_W // 2,
                _DISPLAY_H // 2,
                image=self._photo,
                anchor=tk.CENTER,
            )
        except tk.TclError:
            return

    def _show_error(self, message: str) -> None:
        """Replace the canvas content with a readable error string and
        enable the Open-Externally button if we have a path on hand."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            self._canvas.delete("all")
            self._canvas.create_text(
                _DISPLAY_W // 2,
                _DISPLAY_H // 2 - 10,
                text="Cannot preview this video",
                fill="#FFFFFF",
                font=(FONT_FAMILY, 11, "bold"),
            )
            self._canvas.create_text(
                _DISPLAY_W // 2,
                _DISPLAY_H // 2 + 12,
                text=message,
                fill=COLORS["text_dim"],
                font=(FONT_FAMILY, 9),
                width=_DISPLAY_W - 20,
            )
        except tk.TclError:
            pass
        # If we have a path, give the user a way out via the OS player.
        if self._video_path is not None:
            try:
                self._open_external_btn.config(state=tk.NORMAL)
            except tk.TclError:
                pass
        if self._log_callback is not None:
            try:
                self._log_callback(
                    f"video_inspector: {self._title or 'slot'}: {message}",
                    "warning",
                )
            except Exception:
                pass

    def _on_open_externally(self) -> None:
        if self._video_path is None:
            return
        _open_externally(self._video_path)

    def _on_canvas_resize(self, event) -> None:
        """Re-center every canvas item AND update the cached canvas
        dimensions so the decoder thread re-fits new frames into the
        new size.

        Placeholder ``(no video)`` text + any error text + rendered
        frames were created at absolute coords keyed off the initial
        ``_DISPLAY_W/H = 480/270``. When grid grows the canvas, items
        stay pinned at (240, 135) — visually off-center toward the
        upper-left. We listen for canvas ``<Configure>`` events and:
          1. Update self._canvas_w/h so the next decoded frame is
             resized to fit the NEW canvas with aspect preserved.
          2. Re-center every existing canvas item, preserving each
             item's RELATIVE x/y offset from the original center
             (e.g. the two-line error message keeps its 22px gap).
          3. If a video is currently loaded, request the same frame
             again so it re-renders at the new size right away
             instead of waiting for the next playback tick.
        """
        # Guard against early events (canvas not yet mapped, returns 0).
        if event.width <= 1 or event.height <= 1:
            return
        # Update cached dimensions for the next decode pass.
        old_w, old_h = self._canvas_w, self._canvas_h
        self._canvas_w = event.width
        self._canvas_h = event.height
        new_cx = event.width // 2
        new_cy = event.height // 2
        old_cx = _DISPLAY_W // 2
        old_cy = _DISPLAY_H // 2
        try:
            for item in self._canvas.find_all():
                item_type = self._canvas.type(item)
                if item_type not in ("text", "image"):
                    continue
                # Read current coords; preserve any intentional
                # offset (e.g. error-message line 2 is at y+12).
                cur = self._canvas.coords(item)
                if len(cur) < 2:
                    continue
                cur_x, cur_y = cur[0], cur[1]
                dx = cur_x - old_cx
                dy = cur_y - old_cy
                self._canvas.coords(item, new_cx + dx, new_cy + dy)
        except tk.TclError:
            # Widget destroyed mid-event; safe to ignore.
            pass
        # If a video is loaded AND the canvas size actually changed,
        # request the current frame re-rendered so the user sees the
        # aspect-correct fit immediately rather than after the next
        # play tick. step_to is non-blocking — just enqueues the
        # request for the decoder thread.
        if self._cv2_cap is not None and (
            old_w != self._canvas_w or old_h != self._canvas_h
        ):
            cur_frame = max(0, self._current_frame)
            self.step_to(cur_frame)


# ──────────────────────────────────────────────────────────────────────
# VideoInspectorModal + factory
# ──────────────────────────────────────────────────────────────────────


def open_video_inspector(
    parent: tk.Misc,
    *,
    existing: Optional["VideoInspectorModal"],
    config: dict,
    save_config_fn: Optional[Callable[[], None]] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
    initial_video: Optional[Path] = None,
) -> "VideoInspectorModal":
    """Singleton factory. Focuses the existing modal if alive, else
    constructs a new one. The carousel/main_window keep one reference
    on the parent so reopens don't leak Toplevels or daemon threads.
    """
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.lift()
                existing.focus_set()
                if initial_video is not None:
                    # ALWAYS rescan the folder on reopen, even when the
                    # target folder matches what's currently shown. The
                    # non-blocking workflow has users keep the inspector
                    # open while the queue keeps generating MORE videos
                    # in the same folder; without an unconditional
                    # rescan the listbox + metadata stay frozen on the
                    # snapshot taken at first open, and newly-arrived
                    # variants never appear (Codex PR #43 P2, finding
                    # 3272768125). _refresh_folder is cheap (one
                    # non-recursive iterdir + stem parsing) so the
                    # always-rescan cost is negligible.
                    existing._refresh_folder(Path(initial_video).parent)
                    existing.load_into_slot_a(initial_video)
                else:
                    # Toolbar "Videos" reopen (no preload). If we have
                    # a current folder, rescan it so the listbox picks
                    # up files added since first open. Same Codex P2
                    # reasoning as the initial_video branch above.
                    cur_folder = getattr(existing, "_current_folder", None)
                    if cur_folder is not None:
                        try:
                            existing._refresh_folder(Path(cur_folder))
                        except Exception:
                            # Folder may have been deleted out from
                            # under us; silently ignore (modal stays
                            # showing the last-known snapshot).
                            pass
                return existing
        except tk.TclError:
            pass
    return VideoInspectorModal(
        parent,
        config=config,
        save_config_fn=save_config_fn,
        log_fn=log_fn,
        initial_video=initial_video,
    )


class VideoInspectorModal(tk.Toplevel):
    """A/B video comparison modal. Two VideoFrames + listbox + metadata.

    Modal scope: ``transient(parent)`` + ``grab_set()`` (NOT global) +
    ``focus_set()``. Mirrors SessionManagerDialog so macOS Sonoma
    behaves (no global focus theft).

    Geometry persistence: under the ``video_inspector_window`` key in
    the supplied ``config`` dict, written on destroy() via the
    ``save_config_fn`` callback.
    """

    _GEOMETRY_KEY = "video_inspector_window"

    def __init__(
        self,
        parent: tk.Misc,
        *,
        config: dict,
        save_config_fn: Optional[Callable[[], None]] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
        initial_video: Optional[Path] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._save_config_fn = save_config_fn
        self._log_fn = log_fn
        self._playing = False
        # Float since the FPS-driven _tick refactor; rendered indices
        # cast to int just before cv2 calls.
        self._master_frame: float = 0.0
        self._timer_id: Optional[str] = None
        self._lock_scrub_var = tk.BooleanVar(value=True)
        self._current_folder: Optional[Path] = None
        self._all_videos: List[VideoMetadata] = []
        self._groups: List[VideoGroup] = []
        # Maps listbox row index -> VideoMetadata (None for non-selectable header rows).
        self._row_videos: Dict[int, Optional[VideoMetadata]] = {}
        self._focused_slot: str = "A"

        self.title("Video Inspector")
        self.configure(bg=COLORS["bg_main"])
        # Modal-scope grab (NOT grab_set_global — that would steal focus
        # system-wide on macOS Sonoma).
        try:
            self.transient(parent)
        except tk.TclError:
            pass
        # NOTE: we DON'T call grab_set() here — the inspector is a
        # companion window, not a blocking modal. The user keeps full
        # control of the main GUI. Mirrors how Compare panel behaves.

        self._restore_geometry()
        self._build_ui()
        # Bind close handlers AFTER UI is built.
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.focus_set()

        # Initial population.
        if initial_video is not None:
            self._refresh_folder(initial_video.parent)
            self.load_into_slot_a(initial_video)
        else:
            # Fall back to the parent's last-opened folder if available
            # via the config dict. Otherwise leave empty.
            # Tolerate non-string values in the JSON config (e.g. a
            # stray None or numeric — bad upstream edit, mixed JSON).
            # Path(None) raises TypeError and would abort init.
            last_raw = self._config.get("video_inspector_last_folder")
            last = str(last_raw).strip() if last_raw else ""
            if last:
                self._refresh_folder(Path(last))

    # ── UI construction ──────────────────────────────────────────────

    def _build_ui(self) -> None:
        # New layout (PR #43 user feedback: filename column was crushing
        # long pipeline names like
        #   "front_crop_nano-banana-2-edit_sim82_001_k25tPro_p3_1-oldcam-v24-rppg - 11.89-...mp4"
        # with the right side ALWAYS cut off).
        #
        #   ┌─────────────────────────────────────────────────────────┐
        #   │ Slot A canvas              │ Slot B canvas              │
        #   ├─────────────────────────────────────────────────────────┤
        #   │ Metadata strip (A | B parsed tags)                       │
        #   ├─────────────────────────────────────────────────────────┤
        #   │ Videos in folder:                                        │
        #   │ [ full-width listbox WITH horizontal scrollbar           │
        #   │   showing the entire filename ]                          │
        #   ├─────────────────────────────────────────────────────────┤
        #   │ [▶ Play] [⏮ Restart] [☐ Lock] frame: X/Y  [Load→A/B] [✕]│
        #   └─────────────────────────────────────────────────────────┘

        # ── Video row (top, full width) ──────────────────────────────
        video_panel = tk.Frame(self, bg=COLORS["bg_main"])
        video_panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))
        # 2-column grid with equal weights so Slot A and Slot B ALWAYS
        # get identical widths regardless of inner canvas natural
        # sizes. uniform="slots" is the key — without it Tk's pack
        # would size each column to its child's largest natural width
        # which can drift apart over time (e.g. once a frame loads).
        video_panel.grid_columnconfigure(0, weight=1, uniform="slots")
        video_panel.grid_columnconfigure(1, weight=1, uniform="slots")
        video_panel.grid_rowconfigure(0, weight=1)
        self._frame_a = VideoFrame(
            video_panel, title="Slot A", log_callback=self._log_fn,
            bg=COLORS["bg_main"],
        )
        self._frame_a.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._frame_b = VideoFrame(
            video_panel, title="Slot B", log_callback=self._log_fn,
            bg=COLORS["bg_main"],
        )
        self._frame_b.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        # ── Metadata strip ──────────────────────────────────────────
        self._meta_var = tk.StringVar(value="No video loaded.")
        meta_frame = tk.Frame(self, bg=COLORS["bg_panel"])
        meta_frame.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))
        self._meta_label = tk.Label(
            meta_frame,
            textvariable=self._meta_var,
            bg=COLORS["bg_panel"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=2000,
        )
        self._meta_label.pack(side=tk.TOP, fill=tk.X, padx=8, pady=6)

        # ── Videos-in-folder listbox (full width, BELOW the videos) ──
        list_panel = tk.Frame(self, bg=COLORS["bg_main"])
        list_panel.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 4))
        header_row = tk.Frame(list_panel, bg=COLORS["bg_main"])
        header_row.pack(side=tk.TOP, fill=tk.X)
        tk.Label(
            header_row,
            text="Videos in folder",
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            font=(FONT_FAMILY, 9, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header_row,
            text="  ·  Double-click → A · Shift+Double / Right-click → B",
            bg=COLORS["bg_main"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 8),
        ).pack(side=tk.LEFT)
        # Listbox container: listbox + vertical scrollbar + horizontal
        # scrollbar so the full filename is always reachable.
        list_holder = tk.Frame(list_panel, bg=COLORS["bg_main"])
        list_holder.pack(side=tk.TOP, fill=tk.X, pady=(2, 0))
        list_holder.grid_columnconfigure(0, weight=1)
        list_holder.grid_rowconfigure(0, weight=1)
        self._listbox = tk.Listbox(
            list_holder,
            height=6,  # ~6 rows visible by default; users can resize the modal
            bg=COLORS["bg_input"],
            fg=COLORS["text_light"],
            selectbackground=COLORS["accent_blue"],
            highlightthickness=0,
            relief=tk.FLAT,
            font=(FONT_FAMILY, 9),
            exportselection=False,
            # No fixed width — fill the available horizontal space.
            # Horizontal scrollbar handles overflow for long names.
        )
        self._listbox.grid(row=0, column=0, sticky="nsew")
        vscroll = tk.Scrollbar(
            list_holder, orient=tk.VERTICAL, command=self._listbox.yview,
        )
        vscroll.grid(row=0, column=1, sticky="ns")
        self._listbox.config(yscrollcommand=vscroll.set)
        hscroll = tk.Scrollbar(
            list_holder, orient=tk.HORIZONTAL, command=self._listbox.xview,
        )
        hscroll.grid(row=1, column=0, sticky="ew")
        self._listbox.config(xscrollcommand=hscroll.set)
        self._listbox.bind("<Double-Button-1>", self._on_listbox_double_click)
        # Right-click for macOS trackpad fallback. Shift+Double-click
        # is the explicit cross-platform secondary affordance.
        self._listbox.bind("<Shift-Double-Button-1>", self._on_listbox_shift_double)
        self._listbox.bind("<Button-3>", self._on_listbox_right_click)

        # Toolbar (bottom)
        toolbar = tk.Frame(self, bg=COLORS["bg_main"])
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(0, 8))

        self._play_btn = tk.Button(
            toolbar,
            text="▶ Play",
            command=debounce_command(self._toggle_play, key="vinspect_play"),
            font=(FONT_FAMILY, 9, "bold"),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            disabledforeground=BUTTON_DISABLED_TEXT_COLOR,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            width=10,
        )
        apply_macos_button_fix(self._play_btn)
        self._play_btn.pack(side=tk.LEFT)

        self._restart_btn = tk.Button(
            toolbar,
            text="⏮ Restart",
            command=debounce_command(self._restart, key="vinspect_restart"),
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            width=10,
        )
        apply_macos_button_fix(self._restart_btn)
        self._restart_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._lock_chk = tk.Checkbutton(
            toolbar,
            text="Lock A+B scrub",
            variable=self._lock_scrub_var,
            bg=COLORS["bg_main"],
            fg=COLORS["text_light"],
            selectcolor=COLORS["bg_panel"],
            activebackground=COLORS["bg_main"],
            activeforeground=COLORS["text_light"],
            font=(FONT_FAMILY, 9),
        )
        self._lock_chk.pack(side=tk.LEFT, padx=(12, 0))

        # Frame counter
        self._counter_var = tk.StringVar(value="frame: -/-")
        tk.Label(
            toolbar,
            textvariable=self._counter_var,
            bg=COLORS["bg_main"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        close_btn = tk.Button(
            toolbar,
            text="Close",
            command=self.destroy,
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            width=8,
        )
        apply_macos_button_fix(close_btn)
        close_btn.pack(side=tk.RIGHT)

        # Explicit "Load → A" / "Load → B" buttons live in the toolbar
        # in the new layout. They use the listbox selection so the user
        # can click a row and then click the load button (a third
        # alternative to double-click / shift+double / right-click).
        self._load_b_btn = tk.Button(
            toolbar,
            text="Load → B",
            command=lambda: self._load_selection_into("B"),
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            width=10,
        )
        apply_macos_button_fix(self._load_b_btn)
        self._load_b_btn.pack(side=tk.RIGHT, padx=(0, 6))
        self._load_a_btn = tk.Button(
            toolbar,
            text="Load → A",
            command=lambda: self._load_selection_into("A"),
            font=(FONT_FAMILY, 9),
            bg=COLORS["bg_panel"],
            fg=BUTTON_TEXT_COLOR,
            activebackground=COLORS["bg_hover"],
            activeforeground=BUTTON_TEXT_COLOR,
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=4,
            cursor="hand2",
            width=10,
        )
        apply_macos_button_fix(self._load_a_btn)
        self._load_a_btn.pack(side=tk.RIGHT, padx=(0, 6))

        # Kick off master timer.
        self._schedule_tick()

    # ── Folder / listbox ─────────────────────────────────────────────

    def _refresh_folder(self, folder: Path) -> None:
        folder = Path(folder)
        self._current_folder = folder
        self._config["video_inspector_last_folder"] = str(folder)
        try:
            self.title(f"Video Inspector — {folder.name}")
        except tk.TclError:
            pass
        self._groups = find_video_groups(folder)
        self._all_videos = []
        self._row_videos = {}
        try:
            self._listbox.delete(0, tk.END)
        except tk.TclError:
            return
        for group in self._groups:
            header = f"[ {group.base_stem} ]"
            self._listbox.insert(tk.END, header)
            try:
                self._listbox.itemconfig(
                    self._listbox.size() - 1, fg=COLORS["text_dim"],
                )
            except tk.TclError:
                pass
            self._row_videos[self._listbox.size() - 1] = None
            for vmeta in group.videos:
                self._all_videos.append(vmeta)
                label = self._format_listbox_label(vmeta)
                self._listbox.insert(tk.END, label)
                self._row_videos[self._listbox.size() - 1] = vmeta

    @staticmethod
    def _format_listbox_label(vmeta: VideoMetadata) -> str:
        bits: List[str] = []
        if vmeta.model_short:
            bits.append(vmeta.model_short)
        if vmeta.slot is not None:
            bits.append(f"slot{vmeta.slot}")
        if vmeta.take is not None:
            bits.append(f"take{vmeta.take}")
        if vmeta.oldcam_version is not None:
            bits.append(f"oldcam-v{vmeta.oldcam_version}")
        if vmeta.has_rppg:
            bits.append("rppg")
        tag = " · ".join(bits) if bits else "(raw)"
        return f"    {vmeta.path.name}    [{tag}]"

    def _on_listbox_double_click(self, _event) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        vmeta = self._row_videos.get(sel[0])
        if vmeta is not None:
            self.load_into_slot_a(vmeta.path)

    def _on_listbox_right_click(self, event) -> None:
        # Use the row under the cursor (not the selection) for B-load.
        try:
            idx = self._listbox.nearest(event.y)
        except tk.TclError:
            return
        vmeta = self._row_videos.get(idx)
        if vmeta is not None:
            self.load_into_slot_b(vmeta.path)

    def _on_listbox_shift_double(self, _event) -> str:
        """Shift+Double-click → load slot B. macOS-trackpad-friendly
        alternative to right-click.

        MUST return ``"break"`` to halt Tk's binding chain — without
        it, Shift+Double-1 also propagates to the plain
        ``<Double-Button-1>`` handler (Tk treats modifier+double as
        BOTH a "Shift+Double" AND a "Double") which would then load
        slot A on top of our B load, silently overwriting slot B and
        landing the user with the wrong slot loaded.
        """
        self._load_selection_into("B")
        return "break"

    def _load_selection_into(self, slot: str) -> None:
        """Load the currently-selected listbox row into slot A or B.
        Used by both the explicit Load → A / Load → B buttons and by
        the Shift+Double-click handler. No-op for non-video header rows
        and for empty selections."""
        sel = self._listbox.curselection()
        if not sel:
            return
        vmeta = self._row_videos.get(sel[0])
        if vmeta is None:
            return
        if slot == "B":
            self.load_into_slot_b(vmeta.path)
        else:
            self.load_into_slot_a(vmeta.path)

    # ── Slot loading ────────────────────────────────────────────────

    def load_into_slot_a(self, path: Path) -> bool:
        ok = self._frame_a.load(Path(path))
        # master_frame is a float (FPS-driven). Reset to 0.0 cleanly.
        self._master_frame = 0.0
        self._focused_slot = "A"
        self._refresh_metadata()
        if ok:
            self._frame_a.step_to(0)
            if self._frame_b.is_loaded():
                self._frame_b.step_to(0)
        return ok

    def load_into_slot_b(self, path: Path) -> bool:
        ok = self._frame_b.load(Path(path))
        self._focused_slot = "B"
        self._refresh_metadata()
        if ok:
            # Cast to int for cv2.set(CAP_PROP_POS_FRAMES) — master_frame
            # is a float since the FPS-driven refactor.
            self._frame_b.step_to(int(self._master_frame))
        return ok

    def _refresh_metadata(self) -> None:
        lines: List[str] = []
        for slot, frame in (("A", self._frame_a), ("B", self._frame_b)):
            if not frame.is_loaded() or frame._video_path is None:
                lines.append(f"{slot}: (empty)")
                continue
            meta = parse_video_filename(frame._video_path)
            lines.append(f"{slot}: {self._format_metadata_line(meta)}")
        self._meta_var.set("\n".join(lines))

    @staticmethod
    def _format_metadata_line(m: VideoMetadata) -> str:
        bits: List[str] = []
        if m.model_short:
            bits.append(f"{m.model_short}")
        if m.slot is not None:
            bits.append(f"slot {m.slot}")
        if m.take is not None:
            bits.append(f"take {m.take}")
        if m.oldcam_version is not None:
            bits.append(f"oldcam v{m.oldcam_version}")
        if m.has_rppg:
            if m.rppg_metrics is not None:
                bits.append(
                    "rppg "
                    f"snr={m.rppg_metrics.snr:.2f} "
                    f"phase={m.rppg_metrics.phase:.1f}° "
                    f"temp={m.rppg_metrics.temporal:.2f}"
                )
            else:
                bits.append("rppg (no metrics)")
        if m.similarity is not None:
            bits.append(f"sim {m.similarity}%")
        elif m.similarity_na:
            bits.append("sim n/a")
        return " · ".join(bits) if bits else "(unparsed)"

    # ── Transport ───────────────────────────────────────────────────

    def _toggle_play(self) -> None:
        if not (self._frame_a.is_loaded() or self._frame_b.is_loaded()):
            return
        self._playing = not self._playing
        try:
            self._play_btn.config(text="⏸ Pause" if self._playing else "▶ Play")
        except tk.TclError:
            pass

    def _restart(self) -> None:
        self._master_frame = 0.0
        if self._frame_a.is_loaded():
            self._frame_a.step_to(0)
        if self._frame_b.is_loaded():
            self._frame_b.step_to(0)
        self._update_counter()

    def _schedule_tick(self) -> None:
        try:
            self._timer_id = self.after(_TICK_MS, self._tick)
        except tk.TclError:
            self._timer_id = None

    def _tick(self) -> None:
        # Reschedule LAST (was first; code-reviewer P3, PR #43). The
        # "reschedule first" pattern was conventional but created a
        # narrow race: destroy() could read self._timer_id and cancel
        # the OLD timer, then _tick's _schedule_tick() would install a
        # NEW timer post-cancel. That new timer would fire on a
        # destroyed Toplevel and raise TclError (caught, but the
        # callback ran anyway). Rescheduling at the END means destroy()
        # always sees and cancels the LATEST timer id.
        try:
            if not self._playing:
                self._update_counter()
                return

            # Source-FPS-driven advancement. master_frame is a float;
            # the increment per ~40ms tick is (src_fps * TICK_MS / 1000)
            # so a 60fps source advances ~2.4 frames per tick (rendered
            # at the nearest integer index) and an 8fps source advances
            # ~0.32 frames per tick. Both play at their real wall-clock
            # rate. The "primary" slot for FPS purposes is the focused
            # one when both are loaded, A otherwise, B if only B is
            # loaded.
            primary = self._primary_frame()
            if primary is None or not primary.is_loaded():
                self._playing = False
                try:
                    self._play_btn.config(text="▶ Play")
                except tk.TclError:
                    pass
                return

            fps = primary.get_fps() or 25.0
            step = max(0.04, fps * _TICK_MS / 1000.0)  # floor 1f/25 ticks
            # Codex P2 (3272816291): some containers/codecs return 0
            # for cv2.CAP_PROP_FRAME_COUNT — meaning "unknown length",
            # not "1-frame video". The prior `or 1` substitution made
            # modulo wrap pin `_master_frame` at 0 forever, so videos
            # with unknown counts could never play through. Treat 0
            # as unknown: advance the master frame unboundedly and let
            # the underlying VideoFrame.step_to() ride out the source
            # EOF naturally (cap.read returns False; the worker just
            # stops rendering).
            total = primary.get_frame_count()
            if total > 0:
                self._master_frame = (self._master_frame + step) % total
            else:
                self._master_frame = self._master_frame + step
            idx = int(self._master_frame)

            # Always advance the focused slot (so B-only or B-focused
            # sessions don't freeze, fixing the CodeRabbit Major). The
            # non-focused slot advances only when "Lock A+B scrub" is
            # on. Preserves the lock checkbox's "scrub both together"
            # semantic without freezing B.
            if self._frame_a.is_loaded() and (
                self._focused_slot == "A" or self._lock_scrub_var.get()
            ):
                self._frame_a.step_to(idx)
            if self._frame_b.is_loaded() and (
                self._focused_slot == "B" or self._lock_scrub_var.get()
            ):
                self._frame_b.step_to(idx)

            self._update_counter()
        finally:
            # ALWAYS reschedule, even if the body raised (a transient
            # render error mustn't kill the timer permanently). Guard
            # against destroyed-widget rescheduling via the try/except
            # in _schedule_tick.
            self._schedule_tick()

    def _primary_frame(self) -> Optional["VideoFrame"]:
        """Pick which slot's FPS drives playback timing. Focused slot
        wins; otherwise A if A is loaded, B if only B is loaded."""
        if self._focused_slot == "B" and self._frame_b.is_loaded():
            return self._frame_b
        if self._frame_a.is_loaded():
            return self._frame_a
        if self._frame_b.is_loaded():
            return self._frame_b
        return None

    def _update_counter(self) -> None:
        total = max(
            self._frame_a.get_frame_count(), self._frame_b.get_frame_count()
        )
        if total <= 0:
            self._counter_var.set("frame: -/-")
            return
        # master_frame is a float since the FPS-driven refactor — cast
        # to int for the user-facing counter.
        self._counter_var.set(f"frame: {int(self._master_frame) + 1}/{total}")

    # ── Geometry persistence ────────────────────────────────────────

    def _restore_geometry(self) -> None:
        stored = self._config.get(self._GEOMETRY_KEY)
        # Defaults
        w, h = 1100, 560
        if isinstance(stored, dict):
            try:
                w = int(stored.get("w", w))
                h = int(stored.get("h", h))
            except (TypeError, ValueError):
                pass
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
        except tk.TclError:
            sw, sh = 1920, 1080
        w = max(800, min(w, sw - 40))
        h = max(420, min(h, sh - 80))
        x = (sw - w) // 2
        y = (sh - h) // 2
        if isinstance(stored, dict):
            try:
                rx = int(stored.get("x", x))
                ry = int(stored.get("y", y))
                # Clamp to screen — avoid resurrecting off-screen positions.
                if 0 <= rx <= sw - 100 and 0 <= ry <= sh - 100:
                    x, y = rx, ry
            except (TypeError, ValueError):
                pass
        try:
            self.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass

    def _persist_geometry(self) -> None:
        try:
            w = self.winfo_width()
            h = self.winfo_height()
            x = self.winfo_rootx()
            y = self.winfo_rooty()
        except tk.TclError:
            return
        self._config[self._GEOMETRY_KEY] = {"w": w, "h": h, "x": x, "y": y}
        if self._save_config_fn is not None:
            try:
                self._save_config_fn()
            except Exception:
                logger.exception("video_inspector: save_config_fn failed")

    # ── Lifecycle ───────────────────────────────────────────────────

    def destroy(self) -> None:
        # Master timer first — must die before super().destroy()
        # invalidates self.after_cancel.
        if self._timer_id is not None:
            try:
                self.after_cancel(self._timer_id)
            except tk.TclError:
                pass
            self._timer_id = None
        # Stop both decoder threads & release captures.
        try:
            self._frame_a.clear()
        except Exception:
            logger.exception("video_inspector: clear frame_a failed")
        try:
            self._frame_b.clear()
        except Exception:
            logger.exception("video_inspector: clear frame_b failed")
        # Persist geometry BEFORE we destroy the widget hierarchy.
        try:
            self._persist_geometry()
        except Exception:
            logger.exception("video_inspector: persist_geometry failed")
        try:
            super().destroy()
        except Exception:
            pass
