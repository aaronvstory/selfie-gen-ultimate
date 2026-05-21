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
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from .theme import (
    COLORS,
    FONT_FAMILY,
    TTK_BTN_COMPACT,
    TTK_BTN_PRIMARY,
    TTK_BTN_SECONDARY,
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


# Module-level guard for ttk.Style configuration. ttk.Style is a
# process-global singleton; re-running ``.configure()`` on the same
# style name on every modal open is wasted work and (on long-running
# macOS Tk sessions) has been observed to slow down style lookups.
_INSPECTOR_STYLES_CONFIGURED = False


def _configure_inspector_styles() -> None:
    """Configure the Inspector ttk styles once per process.

    Idempotent — safe to call from every ``VideoInspectorModal.__init__``;
    the actual ``.configure()`` / ``.map()`` calls only run the first
    time. (Code-review on 706466f.)
    """
    global _INSPECTOR_STYLES_CONFIGURED
    if _INSPECTOR_STYLES_CONFIGURED:
        return
    style = ttk.Style()
    style.configure(
        "Inspector.TCheckbutton",
        background=COLORS["bg_main"],
        foreground=COLORS["text_light"],
        font=(FONT_FAMILY, 9),
    )
    style.map(
        "Inspector.TCheckbutton",
        background=[("active", COLORS["bg_main"])],
        foreground=[("active", COLORS["text_light"])],
    )
    _INSPECTOR_STYLES_CONFIGURED = True


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
        # EOF latch for unknown-length sources. Set by the decoder
        # thread when ``cap.read()`` returns False; consumed by the
        # modal master tick to stop ``_playing`` on EOF (without this,
        # the tick would request frames forever, wasting CPU + thread
        # wake-ups). Reset on every fresh ``load()``. Bool assignment
        # is GIL-atomic; no lock needed for cross-thread visibility on
        # CPython. (Gemini MEDIUM on 9d9a473.)
        self._eof_reached = False
        # Live canvas dimensions tracked on <Configure> so the
        # decoder thread can resize frames to the actual visible area
        # (with aspect-preserving fit). Defaults are the _DISPLAY_W/H
        # constants until the first Configure event lands.
        #
        # _canvas_dims is the AUTHORITATIVE source for cross-thread
        # reads: a single tuple object that's reassigned atomically on
        # the Tk thread and read with a single dict-lookup from the
        # decoder thread (atomic under the GIL). The separate
        # _canvas_w / _canvas_h attrs are kept for Tk-thread-only
        # readers (placeholder text drawing, error labels, render
        # centering) — those don't need the atomic-snapshot guarantee
        # because they all run on the Tk thread.
        # Code-reviewer P2 (PR #43, post-79802bc self-review): without
        # the atomic tuple, the decoder could read _canvas_w + the
        # NEWER _canvas_h after a resize event landed mid-iteration,
        # producing a single mis-stretched frame.
        self._canvas_w: int = _DISPLAY_W
        self._canvas_h: int = _DISPLAY_H
        self._canvas_dims: tuple = (_DISPLAY_W, _DISPLAY_H)

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
        self._open_external_btn = ttk.Button(
            self,
            text="Open Externally",
            command=self._on_open_externally,
            style=TTK_BTN_COMPACT,
            state=tk.DISABLED,
        )
        self._open_external_btn.pack(side=tk.TOP, pady=(4, 2))

    # ── Public API ──────────────────────────────────────────────────

    def is_loaded(self) -> bool:
        return self._cv2_cap is not None

    def get_frame_count(self) -> int:
        return self._frame_count

    def get_fps(self) -> float:
        return self._fps

    def get_current_frame(self) -> int:
        return self._current_frame

    def has_reached_eof(self) -> bool:
        """True if the decoder hit EOF on an unknown-length source.

        The modal master tick uses this to halt ``_playing`` so the
        timer stops requesting frames the worker can't decode.
        Reset by every fresh ``load()``.
        """
        return self._eof_reached

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
                logger.debug(
                    "cap.release() after isOpened() False raised",
                    exc_info=True,
                )
            self._show_error("Cannot decode this video")
            return False

        self._cv2_cap = cap
        self._frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        raw_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        # Clamp FPS to a sane range; cv2 returns 0 for some containers.
        self._fps = raw_fps if 1.0 <= raw_fps <= 120.0 else 25.0
        self._current_frame = -1
        self._eof_reached = False  # reset latch for each fresh load

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

    def step_to(self, frame_index: int, *, force: bool = False) -> None:
        """Request the decoder thread render frame ``frame_index``.

        Non-blocking. Drop-oldest semantics — if the worker hasn't
        consumed the prior request yet, replace it (we always want the
        newest frame target, never to play catch-up).

        Dedup (Gemini PR #43 bot pass on 2a32f938): when the master tick
        ticks faster than the source FPS (slow-mo, or a stale-but-still
        request from a paused timer that's about to be killed), repeated
        step_to(N) calls for an already-rendered N would re-decode the
        same frame. Skip the enqueue when frame_index == _current_frame,
        UNLESS the caller passes force=True (_on_canvas_resize uses force
        because the decoded image dimensions are stale on resize even if
        the frame index is unchanged).
        """
        if self._cv2_cap is None:
            return
        if frame_index < 0:
            frame_index = 0
        if self._frame_count > 0 and frame_index >= self._frame_count:
            frame_index = self._frame_count - 1
        if not force and frame_index == self._current_frame:
            return
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
        """Stop the decoder thread, blank the canvas, hand the capture
        off to the worker for release.

        Bumps the generation so any after-callback the (now-doomed)
        worker has already posted to the Tk queue gets rejected by
        _render_pil_image's generation check before touching widgets.

        Critically does NOT call ``cap.release()`` from this thread.
        Codex P1 (3273416655) on PR #43 caught a real close/reload
        race: the decoder thread can be mid-``cap.read()`` (100-200ms
        on H.264) when Tk calls clear(); a synchronous release here
        races the read and crashes OpenCV depending on codec/backend.

        The serialization rule is now: ONLY the decoder thread ever
        calls cap.release(). clear() sets _stop_event + drops a
        sentinel into the request queue; the decoder sees the event
        on its next loop iteration, exits the `while not stop_event`
        loop, and releases its OWN local `cap` reference in a `try
        finally`. Tk thread doesn't wait — the worker is daemon
        and exits within ~250ms (the queue.get timeout).
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

        # Detach our self.* reference to the capture. The actual
        # cap.release() call happens in _decoder_loop's exit path —
        # see Codex P1 comment above for the race-rationale.
        #
        # Note on _cap_lock: the lock here is defensive — the
        # assignment ``self._cv2_cap = None`` is already GIL-atomic and
        # the decoder worker holds its own *local* ``cap`` reference
        # (never reads ``self._cv2_cap``). Readers like ``is_loaded``
        # and ``step_to`` access ``self._cv2_cap`` without the lock,
        # which is safe because (a) the read is a single bytecode op
        # and (b) the generation-id + stop_event scheme ensures stale
        # workers post nothing visible after ``clear()`` returns.
        # The lock is kept ONLY so a future maintainer who genuinely
        # adds a multi-step read/modify/write on ``self._cv2_cap``
        # has a ready-made mutex to grab.
        # (Documented per code-review on 706466f.)
        with self._cap_lock:
            self._cv2_cap = None

        self._photo = None
        self._video_path = None
        self._frame_count = 0
        self._current_frame = -1
        self._eof_reached = False
        try:
            self._canvas.delete("all")
            self._placeholder_id = self._canvas.create_text(
                self._canvas_w // 2,
                self._canvas_h // 2,
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
        # The earlier ``_on_close`` invocation block here was dead code
        # cargo-culted from VideoInspectorModal.destroy() — VideoFrame
        # never sets ``_on_close`` on itself (only the modal does, via
        # its constructor). Removed 2026-05-21 per code-reviewer
        # subagent on 891a2d1: the misleading block could trick a
        # future maintainer into setting ``frame._on_close = cb`` and
        # being surprised when the callback fires during slot clear.
        try:
            self.clear()
        except Exception:
            logger.debug("VideoFrame.clear() during destroy raised", exc_info=True)
        try:
            super().destroy()
        except Exception:
            logger.debug("Tk super().destroy() raised", exc_info=True)

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

        OWNERSHIP: this worker also OWNS the lifecycle of ``cap``.
        clear() does NOT call cap.release() — instead it sets
        stop_event + drops a sentinel, and the ``finally`` block at
        the bottom of this function releases the cap once the worker's
        last cap.read() returns. That serialization is REQUIRED to
        avoid OpenCV close-during-read crashes (Codex P1 / PR #43,
        finding 3273416655). Without it, clear() racing cap.read()
        could trigger OpenCV-backend errors or hard crashes depending
        on codec and platform Tk binding.

        Never builds an ImageTk.PhotoImage here (Tk-thread only).
        Never touches a Tk widget here.
        """
        try:
            from PIL import Image  # type: ignore
        except ImportError:
            self.after(0, lambda: self._show_error("PIL not installed"))
            # Still need to release the cap we were handed even if we
            # can't decode. Single-line try/except for the same reason
            # the main finally has one — never let release() raise.
            # Log at debug so a release-during-PIL-failure regression
            # is diagnosable without spamming the user log
            # (CodeRabbit MAJOR on 19fc413).
            try:
                cap.release()
            except Exception:
                logger.debug(
                    "cap.release() after PIL ImportError raised",
                    exc_info=True,
                )
            return

        try:
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

                # Cap is LOCAL to this generation — passed in as an arg,
                # not read from self.* — so an old worker can't race the
                # new capture. clear() does NOT release this cap from
                # the Tk thread anymore (Codex P1, 3273416655): doing
                # so could race a mid-flight cap.read() on H.264 and
                # crash OpenCV. The release happens in the finally
                # block at the bottom of THIS function, ensuring
                # cap.read() and cap.release() are always serialized
                # on the same thread.
                #
                # Sequential-read fast path: cap.set(CAP_PROP_POS_FRAMES)
                # on H.264/H.265 is O(N) from the nearest keyframe
                # (Gemini #4 PR #43), so we want to skip the seek when
                # the next cap.read() will naturally produce frame_index.
                #
                # cap.get(POS_FRAMES) returns the index a *subsequent*
                # cap.read() WILL produce. After reading frame 5,
                # current=6 — a read now would yield frame 6. So for
                # step_to(N) to render frame N exactly, current MUST
                # equal N. Any mismatch needs a seek.
                #
                # The prior tolerance `abs(current - frame_index) > 1`
                # silently swallowed a one-frame off-by-one: fast-forward
                # from frame 5 to 7 (current=6, requested=7, abs=1) skipped
                # the seek and rendered frame 6 instead of 7. Gemini PR #43
                # caught this.
                try:
                    current = int(cap.get(cv2_mod.CAP_PROP_POS_FRAMES) or 0)
                    # Some OpenCV backends (FFmpeg on certain
                    # platforms, especially right after open/seek
                    # failure) return negative values from
                    # CAP_PROP_POS_FRAMES. A negative ``current``
                    # would skip the seek (since current != N for any
                    # non-negative N) but worse, the sequential-read
                    # fast path's assumption that ``current``
                    # represents "next frame index" is violated. Force
                    # a seek in that case so we get a known good
                    # position. (Gemini MEDIUM on be30379.)
                    if current < 0 or current != frame_index:
                        cap.set(cv2_mod.CAP_PROP_POS_FRAMES, frame_index)
                    ok, frame_bgr = cap.read()
                except Exception:
                    ok, frame_bgr = False, None

                if not ok or frame_bgr is None:
                    # Latch EOF for unknown-length sources so the
                    # modal master tick can halt _playing. We only
                    # latch on UNKNOWN-length (frame_count == 0); for
                    # known-length sources the modal wraps via modulo
                    # and naturally loops, which is the existing
                    # documented behavior. Bool assignment is GIL-
                    # atomic, no lock needed for cross-thread read.
                    # (Gemini MEDIUM on 9d9a473.)
                    #
                    # Generation-gate the write (CodeRabbit MAJOR on
                    # 45007d9): a stale worker that's still draining
                    # after clear()/reload could otherwise flip
                    # ``_eof_reached`` on the NEW load and the modal
                    # would immediately stop playback for a clip that
                    # just opened. Same gate _render_pil_image uses.
                    if (
                        generation_id == self._generation_id
                        and self._frame_count == 0
                    ):
                        self._eof_reached = True
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
                    # Fast-skip corrupt frames with zero dims — would
                    # ZeroDivisionError below and the outer except logs
                    # at error level on every tick. Skip silently
                    # (decoder will retry the next frame).
                    # (Gemini MEDIUM on f0889ac.)
                    if src_w <= 0 or src_h <= 0:
                        continue
                    # Pull the canvas dimensions captured by the last
                    # Configure event. Reading the (w, h) tuple is a
                    # single dict-lookup => atomic under the GIL; this
                    # prevents a mid-decode resize on the Tk thread
                    # from giving us (new_w, old_h) — which would yield
                    # a wrongly-stretched frame for one tick. Tk thread
                    # writes the tuple atomically in _on_canvas_resize.
                    # Falls back to _DISPLAY_W/H before the canvas has
                    # ever been laid out.
                    # Code-reviewer P2 (PR #43, post-79802bc self-review).
                    canvas_dims = self._canvas_dims
                    target_w = canvas_dims[0] or _DISPLAY_W
                    target_h = canvas_dims[1] or _DISPLAY_H
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
                # generation_id flows through so the renderer drops
                # frames from a superseded worker.
                self.after(
                    0, self._render_pil_image, pil_img, frame_index, generation_id,
                )
        finally:
            # Decoder-thread-owned release. Tk's clear() set stop_event
            # and dropped the sentinel; we land here once the in-flight
            # cap.read() returns. Releasing on the same thread that did
            # the last read serializes the OpenCV calls, eliminating
            # the close-during-read race (Codex P1 3273416655). The
            # try/except keeps the worker exit clean even if release
            # itself raises (some OpenCV backends throw on a
            # double-release after error).
            try:
                cap.release()
            except Exception:
                logger.debug("cap.release() in decoder finally raised", exc_info=True)

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
        # Use LIVE canvas dimensions for the center, not the fixed
        # _DISPLAY_W/H constants. Codex P1 (PR #43 bdead49 review):
        # the resize-fit logic already used self._canvas_w/h to pick
        # the resize target, but _render_pil_image was still drawing
        # at (240, 135), so playback re-anchored to the old position
        # every tick after a resize.
        cx = self._canvas_w // 2
        cy = self._canvas_h // 2
        try:
            self._canvas.delete("all")
            self._canvas.create_image(
                cx, cy,
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
        # Use LIVE canvas dimensions so the error text centers on the
        # CURRENT widget size, not the original 480×270 baseline.
        # Codex P1 (PR #43 bdead49 review).
        cx = self._canvas_w // 2
        cy = self._canvas_h // 2
        try:
            self._canvas.delete("all")
            self._canvas.create_text(
                cx, cy - 10,
                text="Cannot preview this video",
                fill="#FFFFFF",
                font=(FONT_FAMILY, 11, "bold"),
            )
            self._canvas.create_text(
                cx, cy + 12,
                text=message,
                fill=COLORS["text_dim"],
                font=(FONT_FAMILY, 9),
                width=max(120, self._canvas_w - 20),
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
        frames were created at absolute coords; on canvas resize we:
          1. Update self._canvas_w/h so the next decoded frame is
             resized to fit the NEW canvas with aspect preserved.
          2. Re-center every existing canvas item, preserving each
             item's RELATIVE x/y offset from the PREVIOUS center
             (e.g. the two-line error message keeps its 22px gap).
             Codex P2 (PR #43 bdead49 review): the old code used
             ``_DISPLAY_W/H`` as the "previous center" for every
             event, which only worked for the FIRST resize from the
             initial 480×270 — subsequent resizes computed offsets
             from the wrong origin and progressively drifted items
             across the canvas. Now the offset baseline is the PRIOR
             canvas size (``old_w/old_h``), so multi-resize sessions
             keep items correctly centered.
          3. If a video is currently loaded, request the same frame
             again so it re-renders at the new size right away
             instead of waiting for the next playback tick.
        """
        # Guard against early events (canvas not yet mapped, returns 0).
        if event.width <= 1 or event.height <= 1:
            return
        # Capture the PRIOR canvas size BEFORE updating cached
        # dimensions — that's the origin we measure offsets from.
        old_w, old_h = self._canvas_w, self._canvas_h
        self._canvas_w = event.width
        self._canvas_h = event.height
        # Atomic snapshot for the decoder thread — single tuple
        # assignment (Tk thread writes; decoder reads via one dict
        # lookup on self._canvas_dims). Without this, a mid-decode
        # resize could leak (new_w, old_h) into one frame's resize
        # math, producing a single wrongly-stretched frame on the
        # transition tick.
        self._canvas_dims = (event.width, event.height)
        new_cx = event.width // 2
        new_cy = event.height // 2
        # Use the PRIOR canvas dimensions as the "old center" for
        # offset preservation, NOT the fixed _DISPLAY_W/H constants
        # (which only matches the initial state). See docstring P2.
        old_cx = old_w // 2
        old_cy = old_h // 2
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
            # force=True bypasses the step_to dedup: the index may be
            # unchanged but the decoded image dimensions are now stale.
            self.step_to(cur_frame, force=True)


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
    on_close: Optional[Callable[[], None]] = None,
) -> "VideoInspectorModal":
    """Singleton factory. Focuses the existing modal if alive, else
    constructs a new one. The carousel/main_window keep one reference
    on the parent so reopens don't leak Toplevels or daemon threads.
    """
    if existing is not None:
        try:
            if existing.winfo_exists():
                # Refresh the on_close callback on every reuse — a
                # caller passing a different closure now would otherwise
                # silently keep the original one. Today main_window
                # always passes the same _clear_inspector_ref bound to
                # the same instance, but future callers (e.g. opening
                # the modal from a different parent) would be subtly
                # broken without this. (Code-reviewer subagent on
                # 891a2d1.)
                if on_close is not None:
                    existing._on_close = on_close
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
        on_close=on_close,
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

    # Soft modulo wrap for unknown-length sources. Hit only when the
    # container reports cv2.CAP_PROP_FRAME_COUNT == 0 (unknown). At
    # 60fps this gives ~4.6h of monotonic growth before wrap — well
    # past any realistic viewing session, but bounded so a multi-day
    # session can't drift the float into precision-loss territory.
    # (Gemini medium PR #43, finding 3277077710.)
    _UNKNOWN_LENGTH_WRAP: float = 1_000_000.0

    # Class-level default — supports test paths that build the modal
    # via __new__ without calling __init__ (same Gemini finding).
    _master_frame: float = 0.0

    def __init__(
        self,
        parent: tk.Misc,
        *,
        config: dict,
        save_config_fn: Optional[Callable[[], None]] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
        initial_video: Optional[Path] = None,
        on_close: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._save_config_fn = save_config_fn
        self._log_fn = log_fn
        # M3 fix (subagent on 2eb16f37): caller passes a callback that
        # nulls its inspector reference when the modal closes. Without
        # this, the parent keeps a stale reference + the destroyed
        # Toplevel widget tree pinned in memory until the next open.
        self._on_close = on_close
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
        # Redesigned 2026-05-21 (user feedback on Windows where the
        # original layout pushed Play/Restart off-screen + made Slot B
        # impossible to load because the "Slot B" pill looked clickable
        # but wasn't):
        #
        #   ┌─────────────────────────────────────────────────────────────┐
        #   │ [▶ Play] [⏮ Restart] [↺ Clear Slots] [☐ Lock] frame: X/Y [✕]│
        #   ├─────────────────────────────────────────────────────────────┤
        #   │ Slot A                       │ Slot B                       │
        #   │ [canvas]                     │ [canvas]                     │
        #   │ [Slot A][Model][pN tN][...]  │ [Slot B][Model][pN tN][...]  │
        #   │ [Load Selected → Slot A]     │ [Load Selected → Slot B]     │
        #   │ [Open Externally]            │ [Open Externally]            │
        #   ├─────────────────────────────────────────────────────────────┤
        #   │ Videos in folder · DoubleClick→A · ShiftDouble/RightClick→B │
        #   │ [ full-width listbox ]                                      │
        #   └─────────────────────────────────────────────────────────────┘
        #
        # Toolbar moved to TOP so Play/Restart are never below the fold
        # regardless of window height (the old bottom-toolbar layout
        # could be pushed off when video_panel.expand=True claimed all
        # remaining vertical space). Pills + Load-Selected-button now
        # live INSIDE each VideoFrame so the slot anchor is visually
        # obvious; user no longer has to hunt for which button maps to
        # which slot.

        # ── Toolbar (TOP — always visible) ─────────────────────────
        self._build_toolbar()

        # ── Video row (middle, full width, expands) ───────────────
        video_panel = tk.Frame(self, bg=COLORS["bg_main"])
        video_panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
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

        # ── Per-slot meta + load button rows ──────────────────────
        # Pills + a single explicit "Load Selected → Slot X" button
        # live INSIDE each VideoFrame so the visual association
        # between metadata + slot canvas is unambiguous. Build them
        # by pack-after the existing canvas + open-external button.
        # The VideoFrame's existing _open_external_btn is already
        # packed (side=TOP), so anything we pack now lands below it
        # — that's not what we want. Re-pack the open-external
        # button to be PAST our new widgets via pack_forget+repack.
        self._meta_row_a = self._attach_slot_extras(self._frame_a, "A")
        self._meta_row_b = self._attach_slot_extras(self._frame_b, "B")

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
            font=(FONT_FAMILY, 9),
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
        # ttk.Scrollbar uses the clam theme — renders dark on macOS,
        # unlike tk.Scrollbar which forces native Aqua white bars and
        # produced ugly bright edges visible against the dark modal bg.
        vscroll = ttk.Scrollbar(
            list_holder, orient=tk.VERTICAL, command=self._listbox.yview,
        )
        vscroll.grid(row=0, column=1, sticky="ns")
        self._listbox.config(yscrollcommand=vscroll.set)
        hscroll = ttk.Scrollbar(
            list_holder, orient=tk.HORIZONTAL, command=self._listbox.xview,
        )
        hscroll.grid(row=1, column=0, sticky="ew")
        self._listbox.config(xscrollcommand=hscroll.set)
        self._listbox.bind("<Double-Button-1>", self._on_listbox_double_click)
        # Shift+Double-click is the explicit cross-platform secondary
        # affordance. Right-click is a platform-specific fallback —
        # ``<Button-3>`` is the Windows/Linux "right mouse button" but
        # macOS Tk reports trackpad secondary-click as either
        # ``<Button-2>`` OR ``<Control-Button-1>`` depending on Tk
        # version. Codex P2 (3273366968) on PR #43: binding only
        # ``<Button-3>`` means the advertised "Right-click -> B"
        # interaction silently fails on macOS. Bind ALL three so the
        # documented interaction works on every platform.
        self._listbox.bind("<Shift-Double-Button-1>", self._on_listbox_shift_double)
        self._listbox.bind("<Button-3>", self._on_listbox_right_click)
        self._listbox.bind("<Button-2>", self._on_listbox_right_click)
        self._listbox.bind("<Control-Button-1>", self._on_listbox_right_click)

        # Toolbar was extracted to _build_toolbar() and packed FIRST
        # at the top of _build_ui (above video_panel). The old inline
        # block lived here, BELOW video_panel + listbox, where Tk's
        # pack(side=TOP, expand=True) on video_panel would push it off
        # the bottom of short windows (user feedback 2026-05-21).

        # Kick off master timer.
        self._schedule_tick()

    # ── Toolbar + per-slot extras (extracted from _build_ui per
    #    user feedback 2026-05-21: toolbar moved to TOP, slot extras
    #    moved INSIDE each VideoFrame) ──────────────────────────────

    def _build_toolbar(self) -> None:
        """Render the top toolbar: Play / Restart / Clear Slots /
        Lock A+B scrub / frame counter / Close.

        Packed at the TOP of the modal so it's always visible
        regardless of window height (the previous bottom-toolbar
        layout could be pushed below the fold by video_panel's
        ``expand=True`` claim). All buttons use ttk + the clam-themed
        TTK_BTN_* styles so they render with the dark theme on macOS.
        """
        toolbar = tk.Frame(self, bg=COLORS["bg_main"])
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 6))

        self._play_btn = ttk.Button(
            toolbar,
            text="▶ Play",
            command=debounce_command(self._toggle_play, key="vinspect_play"),
            style=TTK_BTN_PRIMARY,
            width=10,
        )
        self._play_btn.pack(side=tk.LEFT)

        self._restart_btn = ttk.Button(
            toolbar,
            text="⏮ Restart",
            command=debounce_command(self._restart, key="vinspect_restart"),
            style=TTK_BTN_SECONDARY,
            width=10,
        )
        self._restart_btn.pack(side=tk.LEFT, padx=(6, 0))

        # Start From Scratch — clear both slots in one click
        # (user request 2026-05-21).
        self._clear_btn = ttk.Button(
            toolbar,
            text="↺ Clear Slots",
            command=debounce_command(self._clear_all_slots, key="vinspect_clear"),
            style=TTK_BTN_SECONDARY,
            width=12,
        )
        self._clear_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ttk.Checkbutton instead of tk.Checkbutton: on macOS Aqua the
        # raw tk.Checkbutton reverts to its native white look after the
        # first toggle (same HIView issue as tk.Button — see b3bc7398).
        _configure_inspector_styles()
        self._lock_chk = ttk.Checkbutton(
            toolbar,
            text="Lock A+B scrub",
            variable=self._lock_scrub_var,
            style="Inspector.TCheckbutton",
        )
        self._lock_chk.pack(side=tk.LEFT, padx=(14, 0))

        # Frame counter
        self._counter_var = tk.StringVar(value="frame: -/-")
        tk.Label(
            toolbar,
            textvariable=self._counter_var,
            bg=COLORS["bg_main"],
            fg=COLORS["text_dim"],
            font=(FONT_FAMILY, 9),
        ).pack(side=tk.LEFT, padx=(14, 0))

        close_btn = ttk.Button(
            toolbar,
            text="Close",
            command=self.destroy,
            style=TTK_BTN_SECONDARY,
            width=8,
        )
        close_btn.pack(side=tk.RIGHT)

    def _attach_slot_extras(self, frame: "VideoFrame", slot: str) -> tk.Frame:
        """Build the per-slot pill row + 'Load Selected → Slot X'
        button INSIDE the given VideoFrame, between the canvas and
        the existing 'Open Externally' button.

        Returns the (empty) meta-row Frame so _refresh_metadata can
        populate it. The existing _open_external_btn is repacked
        below our additions so the visual order stays:
            [canvas (expand)]
            [pill row]
            [Load Selected → Slot X]
            [Open Externally]

        User feedback 2026-05-21: previous layout had a separate
        ``_meta_container`` row below BOTH slots, and Load → A/B
        buttons in the toolbar; that decoupling made it ambiguous
        which pill row + button mapped to which slot. Moving both
        inside the VideoFrame ties the slot anchor visually.
        """
        # Re-pack the existing "Open Externally" button to be the
        # LAST sibling so our pills + load button slot above it.
        try:
            frame._open_external_btn.pack_forget()
        except (tk.TclError, AttributeError):
            pass

        # Pill row (empty until _refresh_metadata fills it).
        meta_row = tk.Frame(frame, bg=COLORS["bg_main"])
        meta_row.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(4, 2))

        # Load button — explicit affordance for the listbox selection.
        # User can also use Double-click / Shift+Double / Right-click
        # in the listbox; this button is the visible-anchor variant.
        load_btn = ttk.Button(
            frame,
            text=f"Load Selected → Slot {slot}",
            command=(lambda s=slot: self._load_selection_into(s)),
            style=TTK_BTN_PRIMARY if slot == "A" else TTK_BTN_SECONDARY,
        )
        load_btn.pack(side=tk.TOP, fill=tk.X, padx=8, pady=(2, 4))
        # Stash on the frame for tests + future programmatic access.
        setattr(frame, "_load_into_slot_btn", load_btn)

        # Re-pack the open-externally button (was unpacked above) so
        # it lands LAST. Same options as the original pack in
        # VideoFrame.__init__.
        try:
            frame._open_external_btn.pack(side=tk.TOP, pady=(4, 2))
        except (tk.TclError, AttributeError):
            pass

        return meta_row

    def _clear_all_slots(self) -> None:
        """Reset both VideoFrames to empty state — the 'Start From
        Scratch' affordance. Also resets the master frame counter
        and pauses playback. (User request 2026-05-21.)"""
        self._playing = False
        try:
            self._play_btn.config(text="▶ Play")
        except tk.TclError:
            pass
        try:
            self._frame_a.clear()
        except Exception:
            logger.debug("clear slot A failed", exc_info=True)
        try:
            self._frame_b.clear()
        except Exception:
            logger.debug("clear slot B failed", exc_info=True)
        self._master_frame = 0.0
        self._focused_slot = "A"
        self._refresh_metadata()
        self._update_counter()

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
        """Re-render the pill rows for both slots.

        Each row is wiped + rebuilt per call. Cheap (a handful of tiny
        frames + labels per slot) and keeps the implementation simple
        — no diff-and-mutate needed.
        """
        for slot_letter, frame, row in (
            ("A", self._frame_a, self._meta_row_a),
            ("B", self._frame_b, self._meta_row_b),
        ):
            for child in row.winfo_children():
                child.destroy()
            self._render_pills_for_slot(row, slot_letter, frame)

    # Model-short → human-readable display name. Reverse of
    # queue_manager._model_short_from_endpoint. Used by the slot
    # pill row so the user sees "Kling 2.5 Turbo Std" instead of
    # "k25tStd".
    _MODEL_SHORT_DISPLAY = {
        "k25tPro":   "Kling 2.5 Turbo Pro",
        "k25tStd":   "Kling 2.5 Turbo Std",
        "k25tMaster":"Kling 2.5 Turbo Master",
        "k25":       "Kling 2.5",
        "k26pro":    "Kling 2.6 Pro",
        "k26std":    "Kling 2.6 Std",
        "k26master": "Kling 2.6 Master",
        "k30pro":    "Kling 3.0 Pro",
        "k30std":    "Kling 3.0 Std",
        "k21pro":    "Kling 2.1 Pro",
        "k21std":    "Kling 2.1 Std",
        "k20pro":    "Kling 2.0 Pro",
        "k20std":    "Kling 2.0 Std",
        "k16pro":    "Kling 1.6 Pro",
        "k16std":    "Kling 1.6 Std",
        "k15pro":    "Kling 1.5 Pro",
        "k15std":    "Kling 1.5 Std",
        "kO1":       "Kling O1",
        "veo3":      "Veo 3",
        "veo":       "Veo",
        "wan25":     "Wan 2.5",
        "wan":       "Wan",
        "ovi":       "Ovi",
        "ltx2":      "LTX 2",
        "pix5":      "Pixverse v5",
        "pixverse":  "Pixverse",
        "hunyuan":   "Hunyuan",
        "minimax":   "Minimax",
    }

    def _model_short_display(self, short: Optional[str]) -> str:
        """Return the human-readable name for a model_short code."""
        if not short:
            return "model ?"
        return self._MODEL_SHORT_DISPLAY.get(short, short)

    def _make_pill(
        self,
        parent: tk.Misc,
        text: str,
        *,
        bg: str = "#3C3C41",
        fg: str = "#E8E8E8",
        tooltip: Optional[str] = None,
    ) -> tk.Frame:
        """Build a single rounded-ish pill. ttk-Style rounded corners
        aren't supported by Tk, so we approximate with a flat bg-tinted
        Frame + 1px border + tight padding. Reads as a pill at the
        glance distance the meta-strip is used from.
        """
        pill = tk.Frame(
            parent, bg=bg,
            highlightbackground="#4A4A4F", highlightthickness=1, bd=0,
        )
        tk.Label(
            pill, text=text, bg=bg, fg=fg,
            font=(FONT_FAMILY, 10, "bold"),
            padx=8, pady=3,
        ).pack()
        if tooltip:
            # HoverTooltip is defined in config_panel.py — lazy-import
            # to avoid a circular dep (config_panel imports nothing here).
            try:
                from .config_panel import HoverTooltip
                HoverTooltip(pill, lambda _t=tooltip: _t)
            except Exception:
                # GUI must stay responsive even if tooltip wiring fails;
                # log at debug so we can diagnose regressions without
                # spamming the user log (CodeRabbit minor on 253a9b4).
                logger.debug(
                    "Failed to attach HoverTooltip to VideoInspectorModal pill",
                    exc_info=True,
                )
        return pill

    def _render_pills_for_slot(
        self,
        row: tk.Frame,
        slot_letter: str,
        frame: "VideoFrame",
    ) -> None:
        """Build the pill row for one slot. Order:
          [Slot A/B] [Model] [pN tN] [Looped?] [Oldcam vN?] [rPPG?] [Sim N%?]
        Empty slot just shows a dim "(empty)" label.
        """
        # Slot letter pill — always present, larger + bolder than data
        # pills so the eye finds the row anchor instantly.
        slot_pill = tk.Frame(
            row, bg=COLORS["accent_blue"],
            highlightbackground="#4A4A4F", highlightthickness=1, bd=0,
        )
        tk.Label(
            slot_pill, text=f"Slot {slot_letter}",
            bg=COLORS["accent_blue"], fg="#FFFFFF",
            font=(FONT_FAMILY, 10, "bold"),
            padx=10, pady=3,
        ).pack()
        slot_pill.pack(side=tk.LEFT, padx=(0, 6))

        if not frame.is_loaded() or frame._video_path is None:
            tk.Label(
                row, text="(empty)",
                bg=COLORS["bg_panel"], fg=COLORS["text_dim"],
                font=(FONT_FAMILY, 10, "italic"),
            ).pack(side=tk.LEFT, padx=(2, 0))
            return

        meta = parse_video_filename(frame._video_path)

        # Model pill — human-readable name (e.g. "Kling 2.5 Turbo Std")
        # M2 (subagent on cac29c8f): suppress the model pill on
        # non-pipeline filenames (user-renamed mp4, .mov, .webm, etc.).
        # Render a single "[unrecognized format]" pill instead so the
        # user sees we couldn't parse the name + no other metadata
        # pills follow (slot/take/oldcam/rppg/sim are all derived from
        # the pipeline naming convention).
        if meta.model_short is None:
            self._make_pill(
                row, "[unrecognized format]",
                bg="#4A4A4F", fg="#A0A0A5",
                tooltip=(
                    "Filename doesn't match the pipeline naming "
                    "convention\n(stem_modelShort_pN_M.mp4 + optional "
                    "oldcam/rPPG/looped\nsuffixes), so model/slot/take/"
                    "oldcam/rPPG/sim metadata\nisn't available."
                ),
            ).pack(side=tk.LEFT, padx=(0, 4))
            return
        model_display = self._model_short_display(meta.model_short)
        self._make_pill(
            row, model_display,
            bg="#2A4A8C", fg="#E0E8FF",
            tooltip=(
                f"Model: {model_display}\n"
                f"Short code in filename: {meta.model_short}"
            ),
        ).pack(side=tk.LEFT, padx=(0, 4))

        # Slot/take pill — concise (e.g. "p3 · t1")
        if meta.slot is not None or meta.take is not None:
            slot_take = []
            if meta.slot is not None:
                slot_take.append(f"p{meta.slot}")
            if meta.take is not None:
                slot_take.append(f"t{meta.take}")
            self._make_pill(
                row, " · ".join(slot_take),
                bg="#3C3C41",
                tooltip="Prompt slot · take number (the N in the\n"
                        "filename suffix _pN_M).",
            ).pack(side=tk.LEFT, padx=(0, 4))

        # Looped pill (small, dim — informational)
        if meta.is_looped:
            self._make_pill(
                row, "looped",
                bg="#2A4A6A", fg="#A0C8E8",
                tooltip="FFmpeg ping-pong loop applied. Doubles the\n"
                        "clip length by appending a reverse copy so\n"
                        "the video can play in a continuous loop.",
            ).pack(side=tk.LEFT, padx=(0, 4))

        # Oldcam pill
        if meta.oldcam_version is not None:
            v = meta.oldcam_version
            oldcam_tooltip = (
                f"Oldcam v{v} applied. Adds camera/sensor imperfections\n"
                f"(rolling shutter, AWB drift, OIS spring-damper, etc.)\n"
                f"to make AI-generated frames read as filmed footage.\n"
                f"Hover the (i) icon next to \"Oldcam injection\" in\n"
                f"Step 3 for the full per-version breakdown."
            )
            self._make_pill(
                row, f"oldcam v{v}",
                bg="#5A3A7A", fg="#E0D0F0",  # violet (matches Step 3 frame)
                tooltip=oldcam_tooltip,
            ).pack(side=tk.LEFT, padx=(0, 4))

        # rPPG pill (with metrics if present)
        if meta.has_rppg:
            metrics = meta.rppg_metrics
            if metrics is not None:
                # Compact metric form: snr=XX.X · ph=YY.Y°
                rppg_text = (
                    f"rppg · snr {metrics.snr:.1f} · ph {metrics.phase:.0f}°"
                )
                rppg_tip = (
                    f"rPPG injection metrics (from filename or sidecar):\n"
                    f"  SNR (Signal-to-Noise Ratio): {metrics.snr:.2f}\n"
                    f"  Phase offset: {metrics.phase:.1f}°\n"
                    f"  Temporal correlation: {metrics.temporal:.3f}\n"
                    f"  Motion stability: {metrics.motion:.3f}\n"
                    f"  Harmonic energy: {metrics.harmonic:.3f}\n"
                    f"Higher SNR = stronger physiological signal\n"
                    f"detectable by passive rPPG (Persona). Source:\n"
                    f"{meta.rppg_metrics_source or '?'}."
                )
            else:
                rppg_text = "rppg"
                rppg_tip = (
                    "rPPG pulse injected. Metrics not present in this\n"
                    "filename (rppg_metrics_in_filename was off when\n"
                    "this video ran). Sidecar JSON may have details."
                )
            self._make_pill(
                row, rppg_text,
                bg="#7A4A1F", fg="#F0D8B0",  # orange (matches Step 3 frame)
                tooltip=rppg_tip,
            ).pack(side=tk.LEFT, padx=(0, 4))

        # Similarity pill — color graded green-pass / amber-borderline /
        # red-fail based on the standard 80-pass threshold.
        if meta.similarity is not None:
            sim = meta.similarity
            if sim >= 80:
                sim_bg, sim_fg = "#2A6A2A", "#D8F0D8"  # green
            elif sim >= 60:
                sim_bg, sim_fg = "#7A6A1F", "#F0E8B0"  # amber
            else:
                sim_bg, sim_fg = "#7A2A2A", "#F0C8C8"  # red
            self._make_pill(
                row, f"sim {sim}%",
                bg=sim_bg, fg=sim_fg,
                tooltip=(
                    f"Face similarity vs. the reference image: {sim}%.\n"
                    "Pass threshold: 80% (green). 60-79% = amber\n"
                    "(borderline). <60% = red (fail). Computed during\n"
                    "selfie generation; embedded in the filename."
                ),
            ).pack(side=tk.LEFT, padx=(0, 4))
        elif meta.similarity_na:
            self._make_pill(
                row, "sim n/a",
                bg="#4A4A4F", fg="#A0A0A5",
                tooltip="Similarity score not computed for this clip.",
            ).pack(side=tk.LEFT, padx=(0, 4))

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
        was_playing = self._playing
        self._playing = not self._playing
        try:
            self._play_btn.config(text="⏸ Pause" if self._playing else "▶ Play")
        except tk.TclError:
            pass
        # If resuming from pause AND the timer is stopped (paused-exit
        # cleared it), restart the ticker. The destroy()-race fix from
        # PR #43 P3 is preserved: we only schedule when no timer is
        # currently set, and the wrapped after() in _schedule_tick still
        # catches TclError on destroyed widgets.
        if self._playing and not was_playing and self._timer_id is None:
            self._schedule_tick()

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
        # Lifecycle guard: if the modal was destroyed between the
        # last after() scheduling and this callback firing,
        # winfo_exists() returns 0 and we exit cleanly. Without
        # this, any sub-call below that touches a destroyed widget
        # (config(), winfo_*, after_cancel) would raise TclError —
        # caught downstream, but the callback still runs. Bailing
        # immediately also prevents the finally-block reschedule
        # from installing a new timer on the dead Toplevel.
        # (Gemini MEDIUM on 7096ff8.)
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return

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
                # Pause-state exit BEFORE the finally-reschedule sets a
                # new timer. Pre-fix: empty ticks rescheduled every 40ms
                # forever while paused (~25 Hz idle wakeups). Post-fix:
                # the finally block sees self._timer_id=None (set below)
                # and skips the reschedule; _toggle_play restarts the
                # ticker when the user resumes.
                self._update_counter()
                self._timer_id = None
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
                # Clear _timer_id too — the finally-block reschedules
                # when EITHER ``_timer_id is not None`` OR ``_playing``
                # is True. Without this clear, the finally fires one
                # more redundant tick before the timer actually stops.
                # Mirrors the paused-exit branch above. (Gemini MEDIUM
                # on c199b11.)
                self._timer_id = None
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
                # Unknown-length source: wrap at _UNKNOWN_LENGTH_WRAP so
                # the float stays bounded across multi-hour sessions.
                # VideoFrame.step_to() handles the actual EOF (cap.read
                # returns False; worker stops rendering).
                # (Gemini medium PR #43, finding 3277077710.)
                #
                # If the decoder has latched EOF on this unknown-length
                # source, stop _playing so the tick stops requesting
                # frames the worker can't decode (Gemini MEDIUM on
                # 9d9a473 — was burning ~25 wakeups/sec uselessly).
                if primary.has_reached_eof():
                    self._playing = False
                    # Clear _timer_id so the finally-block reschedule
                    # doesn't fire one more redundant tick before
                    # stopping (Gemini MEDIUM on c199b11). Mirrors the
                    # paused-exit and primary-lost branches above.
                    self._timer_id = None
                    try:
                        self._play_btn.config(text="▶ Play")
                    except tk.TclError:
                        pass
                    return
                self._master_frame = (
                    self._master_frame + step
                ) % self._UNKNOWN_LENGTH_WRAP
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
            # Reschedule unless the paused-exit branch explicitly
            # cleared timer_id (or destroy() did). Pre-fix: empty
            # paused ticks rescheduled forever. The try/except in
            # _schedule_tick still guards against the destroyed-widget
            # case where after() raises TclError.
            if self._timer_id is not None or self._playing:
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
        # Defaults bumped 2026-05-21 per user feedback: prior 1100x560
        # default + 800x420 minimum was too short on Windows — the
        # bottom toolbar got pushed off-screen. New layout has the
        # toolbar at TOP and per-slot extras inside each VideoFrame,
        # which needs more vertical room. Defaults of 1280x780 give
        # the per-slot pill rows + load buttons + listbox + toolbar
        # all room to breathe at first open.
        w, h = 1280, 780
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
        # Minimums bumped from 800x420 → 1000x640 so the per-slot
        # extras + toolbar always fit (user request 2026-05-21).
        w = max(1000, min(w, sw - 40))
        h = max(640, min(h, sh - 80))
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
        # invalidates self.after_cancel. Setting _playing=False stops
        # the finally-block reschedule inside any in-flight _tick from
        # installing a NEW timer on the dead Toplevel between this
        # cancel and the actual destroy() landing. The winfo_exists()
        # guard at the top of _tick (c199b11) already catches the
        # post-destroy case, but stopping playback here closes the
        # race tighter. (Gemini MEDIUM on f0889ac.)
        self._playing = False
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
        # Notify the caller (e.g. main_window._clear_inspector_ref) so it
        # can null its singleton reference. WM_DELETE_WINDOW is wired to
        # self.destroy(), so without this hook the parent's reference is
        # only cleared when the parent calls destroy() programmatically
        # — the user closing via the X button leaves a stale destroyed
        # Toplevel pinned until the next reopen. One-shot: clear after
        # invoking to keep re-entry safe.
        # (Codex P3 PR #43, finding 3277032160.)
        if getattr(self, "_on_close", None) is not None:
            try:
                self._on_close()
            except Exception:
                logger.debug("video_inspector on_close raised", exc_info=True)
            self._on_close = None
        try:
            super().destroy()
        except Exception:
            pass
