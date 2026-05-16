"""Tkinter (ttk) GUI: folder pick → ranked comparison table → scoring.

Workflow:
- Picking a folder discovers videos AND auto-loads any existing
  ``<video>.ext.json`` sidecars (no API calls), so prior runs show up
  immediately.
- "Score Selected" only calls the API for selected videos that have no
  result yet (incremental); rankings recompute over loaded + new.
- Click any metric column header to re-rank by it (default Frame Mean,
  ascending = most authentic first). Top 1/2/3 get 🥇🥈🥉.

Threading model: discovery and scoring run on daemon worker threads.
Worker threads NEVER touch Tk directly — not even ``root.after()``, which
is itself not thread-safe (``tk.createcommand``). Workers push callables
onto a ``queue.Queue`` drained by a poller scheduled on the **main** thread
from ``__init__``. This is the only safe cross-thread Tk pattern.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable, Optional

from . import client, scoring, theme
from .discovery import VideoItem, discover

CHECK_ON = "☑"
CHECK_OFF = "☐"
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Metric columns the user can click to re-rank by (col id -> scoring key).
SORTABLE = {
    "frame_mean": "frame_mean",
    "frame_min": "frame_min",
    "frame_max": "frame_max",
    "chunk_mean": "chunk_mean",
}
_COLS = (
    "group",
    "frame_mean",
    "frame_min",
    "frame_max",
    "chunk_mean",
    "frames",
    "verdict",
    "status",
)
_HEADINGS = {
    "group": "Group",
    "frame_mean": "Frame Mean",
    "frame_min": "Frame Min",
    "frame_max": "Frame Max",
    "chunk_mean": "Chunk Mean",
    "frames": "Frames",
    "verdict": "Verdict (raw)",
    "status": "Status",
}


def _num(v) -> str:
    return "—" if v is None else f"{v:.4f}"


class _Row:
    """One video's model state (selection + optional Result)."""

    __slots__ = ("item", "checked", "result")

    def __init__(self, item: VideoItem) -> None:
        self.item = item
        self.checked = True
        self.result: Optional[scoring.Result] = None

    @property
    def scored(self) -> bool:
        return self.result is not None and self.result.ok


class ResembleScoreGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("resemble-score — deepfake scoring & comparison")
        self.root.geometry("1180x740")
        self.root.minsize(960, 560)
        theme.apply_dark_ttk(self.root)

        self.folder: Optional[Path] = None
        self.recursive = tk.BooleanVar(value=True)
        # path-string -> _Row  (preserves discovery order for grouping)
        self._rows: dict[str, _Row] = {}
        # tree-item-id -> path-string  (only for video rows)
        self._iid_to_path: dict[str, str] = {}
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._sort_by = scoring.DEFAULT_SORT
        self._sort_desc = False

        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()

        self._build()
        self.root.after(50, self._drain_ui_queue)

    # ---- thread-safe UI marshalling --------------------------------------
    def _post(self, fn: Callable, *args) -> None:
        self._ui_queue.put(lambda: fn(*args))

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                cb = self._ui_queue.get_nowait()
                try:
                    cb()
                except Exception:
                    import traceback

                    traceback.print_exc()
        except queue.Empty:
            pass
        self.root.after(50, self._drain_ui_queue)

    # ---- layout -----------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 10, 12, 2))
        top.pack(fill="x")
        ttk.Label(
            top, text="resemble-score", style="Title.TLabel"
        ).pack(side="left")
        ttk.Label(
            top, text="deepfake scoring & comparison", style="Muted.TLabel"
        ).pack(side="left", padx=12)

        legend = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        legend.pack(fill="x")
        ttk.Label(
            legend,
            text=(
                "All scores 0–1 (deepfake probability) — lower = more "
                "authentic.  Existing results load automatically; Score "
                "Selected only calls the API for videos without a result.  "
                "Click a metric header to re-rank (🥇🥈🥉 = top 3).  "
                "Verdict is Resemble's raw label and rounds to Fake/1.0 "
                "for most AI clips, so it can't compare variants — the "
                "per-frame columns can."
            ),
            style="Muted.TLabel",
            wraplength=1620,
            justify="left",
        ).pack(side="left")

        bar = ttk.Frame(self.root, padding=(10, 0))
        bar.pack(fill="x")
        self.pick_btn = ttk.Button(
            bar, text="Pick Folder…", command=self._pick_folder
        )
        self.pick_btn.pack(side="left")
        self.recursive_cb = ttk.Checkbutton(
            bar,
            text="Recursive",
            variable=self.recursive,
            command=self._on_recursive_toggle,
        )
        self.recursive_cb.pack(side="left", padx=8)
        self.folder_lbl = ttk.Label(
            bar, text="(no folder selected)", style="Muted.TLabel"
        )
        self.folder_lbl.pack(side="left", padx=8)

        selbar = ttk.Frame(self.root, padding=(10, 6))
        selbar.pack(fill="x")
        self.all_btn = ttk.Button(
            selbar, text="Select All", command=lambda: self._set_all(True)
        )
        self.all_btn.pack(side="left")
        self.none_btn = ttk.Button(
            selbar, text="Select None", command=lambda: self._set_all(False)
        )
        self.none_btn.pack(side="left", padx=6)
        self.oldcam_btn = ttk.Button(
            selbar,
            text="Select Oldcam",
            command=lambda: self._select_group(oldcam=True),
        )
        self.oldcam_btn.pack(side="left", padx=6)
        self.orig_btn = ttk.Button(
            selbar,
            text="Select Original",
            command=lambda: self._select_group(oldcam=False),
        )
        self.orig_btn.pack(side="left", padx=6)
        self.unscored_btn = ttk.Button(
            selbar,
            text="Select Unscored",
            command=self._select_unscored,
        )
        self.unscored_btn.pack(side="left", padx=6)

        tree_wrap = ttk.Frame(self.root, padding=(12, 6))
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_wrap, columns=_COLS, show="tree headings",
            selectmode="none",
        )
        self.tree.heading("#0", text="  ☑  Rank · File")
        for col in _COLS:
            label = _HEADINGS[col]
            if col in SORTABLE:
                self.tree.heading(
                    col,
                    text=label,
                    command=lambda c=col: self._on_header_click(c),
                )
            else:
                self.tree.heading(col, text=label)
        self.tree.column("#0", width=420, anchor="w", stretch=True)
        self.tree.column("group", width=110, anchor="w")
        self.tree.column("frame_mean", width=108, anchor="e")
        self.tree.column("frame_min", width=92, anchor="e")
        self.tree.column("frame_max", width=92, anchor="e")
        self.tree.column("chunk_mean", width=98, anchor="e")
        self.tree.column("frames", width=64, anchor="e")
        self.tree.column("verdict", width=120, anchor="w")
        self.tree.column("status", width=88, anchor="w")
        self.tree.tag_configure(
            "gold", background=theme.WINNER_BG, foreground=theme.WINNER_FG
        )
        self.tree.tag_configure("silver", background="#2c3340")
        self.tree.tag_configure("bronze", background="#33302a")
        self.tree.tag_configure("error", foreground=theme.ERROR_FG)
        self.tree.tag_configure("odd", background=theme.ROW_ALT)
        vsb = ttk.Scrollbar(
            tree_wrap, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)

        bottom = ttk.Frame(self.root, padding=10)
        bottom.pack(fill="x")
        self.score_btn = ttk.Button(
            bottom, text="Score Selected", command=self._start_scoring
        )
        self.score_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            bottom, text="Cancel", command=self._cancel_scoring,
            state="disabled",
        )
        self.cancel_btn.pack(side="left", padx=6)
        self.status_lbl = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.status_lbl.pack(side="left", padx=12)

    # ---- folder + discovery ----------------------------------------------
    def _pick_folder(self) -> None:
        from tk_dialogs import select_directory

        picked = select_directory(
            parent=self.root, title="Select a folder of videos"
        )
        if not picked:
            return
        self.folder = Path(picked)
        self.folder_lbl.configure(text=str(self.folder))
        self._start_discovery()

    def _on_recursive_toggle(self) -> None:
        if self.folder is not None:
            self._start_discovery()

    def _start_discovery(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        if not self.folder:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._rows.clear()
        self._iid_to_path.clear()
        self._set_controls(busy=True)
        self.status_lbl.configure(text="Scanning folder…")
        folder = self.folder
        recursive = self.recursive.get()
        self._worker = threading.Thread(
            target=self._discovery_run,
            args=(folder, recursive),
            daemon=True,
        )
        self._worker.start()

    def _discovery_run(self, folder: Path, recursive: bool) -> None:
        try:
            items = discover(folder, recursive=recursive)
        except (NotADirectoryError, FileNotFoundError, OSError) as e:
            self._post(self._on_discovery_error, str(e))
            return
        # Load existing sidecar results on the worker (disk IO, no Tk).
        loaded: list[tuple[VideoItem, Optional[scoring.Result]]] = []
        for it in items:
            try:
                res = scoring.load_existing_result(it.path, it.group)
            except Exception:
                res = None
            loaded.append((it, res))
        self._post(self._populate, loaded)

    def _on_discovery_error(self, msg: str) -> None:
        self._set_controls(busy=False)
        self.status_lbl.configure(text="Discovery failed.")
        messagebox.showerror("Discovery failed", msg)

    def _populate(self, loaded) -> None:
        self._set_controls(busy=False)
        self._rows.clear()
        for it, res in loaded:
            row = _Row(it)
            row.result = res
            self._rows[str(it.path)] = row
        if not self._rows:
            self.status_lbl.configure(
                text="No videos found in this folder."
            )
            return
        self._rebuild_tree()
        n = len(self._rows)
        have = sum(1 for r in self._rows.values() if r.scored)
        msg = f"{n} video(s) found"
        if have:
            msg += f" — {have} already have results (loaded)"
            if have < n:
                msg += f", {n - have} need scoring"
        else:
            msg += " — none scored yet"
        self.status_lbl.configure(text=msg + ".")

    # ---- tree rendering (single source of truth) -------------------------
    def _ordered_rows(self) -> list[_Row]:
        """Scored rows ranked by the active metric, then unscored, grouped."""
        scored = [r for r in self._rows.values() if r.scored]
        unscored = [r for r in self._rows.values() if not r.scored]
        results = [r.result for r in scored if r.result is not None]
        scoring.rank(
            results, by=self._sort_by, descending=self._sort_desc
        )

        # rank() set .rank on each Result in place; order rows to match.
        def _rank_of(r: _Row) -> int:
            return (
                r.result.rank
                if r.result is not None and r.result.rank is not None
                else 1_000_000
            )

        scored.sort(key=_rank_of)
        unscored.sort(
            key=lambda r: (r.item.group, r.item.name.lower())
        )
        return scored + unscored

    def _rebuild_tree(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._iid_to_path.clear()

        # Heading arrows reflect the active sort.
        for col in _COLS:
            base = _HEADINGS[col]
            if col == self._sort_by:
                base += " ▼" if self._sort_desc else " ▲"
            if col in SORTABLE or col in _HEADINGS:
                self.tree.heading(col, text=base)

        groups: dict[str, str] = {}
        for idx, row in enumerate(self._ordered_rows()):
            it = row.item
            if it.group not in groups:
                groups[it.group] = self.tree.insert(
                    "", "end", text=it.group,
                    values=("",) * len(_COLS), open=True,
                )
            res = row.result
            mark = CHECK_ON if row.checked else CHECK_OFF
            tags: tuple = ()
            if res is not None and res.ok:
                medal = MEDALS.get(res.rank or 0, "")
                label = (
                    f"  {mark}  {medal} #{res.rank}  {it.name}"
                    if medal
                    else f"  {mark}  #{res.rank}  {it.name}"
                )
                verdict = (
                    f"{res.verdict_label} ({_num(res.verdict_score)})"
                    if res.verdict_label
                    else "—"
                )
                vals = (
                    it.group,
                    _num(res.frame_mean),
                    _num(res.frame_min),
                    _num(res.frame_max),
                    _num(res.chunk_mean),
                    str(res.frame_count or "—"),
                    verdict,
                    "loaded" if res.status == "loaded" else "ok",
                )
                if res.rank == 1:
                    tags = ("gold",)
                elif res.rank == 2:
                    tags = ("silver",)
                elif res.rank == 3:
                    tags = ("bronze",)
                elif idx % 2:
                    tags = ("odd",)
            else:
                label = f"  {mark}  {it.name}"
                status = "—"
                if res is not None and res.status == "error":
                    status = "error"
                    tags = ("error",)
                elif idx % 2:
                    tags = ("odd",)
                vals = (it.group, "", "", "", "", "", "", status)

            iid = self.tree.insert(
                groups[it.group], "end", text=label, values=vals,
                tags=tags,
            )
            self._iid_to_path[iid] = str(it.path)

    def _on_header_click(self, col: str) -> None:
        if col not in SORTABLE:
            return
        if self._sort_by == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_by = col
            self._sort_desc = False
        self._rebuild_tree()

    # ---- checkbox handling ------------------------------------------------
    def _on_click(self, event: tk.Event) -> None:
        if self._worker and self._worker.is_alive():
            return
        iid = self.tree.identify_row(event.y)
        path = self._iid_to_path.get(iid)
        if path is None:
            return
        row = self._rows[path]
        row.checked = not row.checked
        self._rebuild_tree()

    def _set_all(self, checked: bool) -> None:
        for r in self._rows.values():
            r.checked = checked
        self._rebuild_tree()

    def _select_group(self, *, oldcam: bool) -> None:
        for r in self._rows.values():
            r.checked = (r.item.version is not None) == oldcam
        self._rebuild_tree()

    def _select_unscored(self) -> None:
        for r in self._rows.values():
            r.checked = not r.scored
        self._rebuild_tree()

    # ---- scoring (threaded, incremental) ---------------------------------
    def _set_controls(self, *, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in (
            self.pick_btn,
            self.all_btn,
            self.none_btn,
            self.oldcam_btn,
            self.orig_btn,
            self.unscored_btn,
            self.score_btn,
        ):
            btn.configure(state=state)
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def _start_scoring(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        try:
            api_key = client.resolve_api_key()
        except RuntimeError as e:
            messagebox.showerror("Missing API key", str(e))
            return

        selected = [r for r in self._rows.values() if r.checked]
        if not selected:
            messagebox.showinfo(
                "Nothing selected", "Tick at least one video to score."
            )
            return

        # Incremental: only API the selected videos with no result yet.
        todo = [r for r in selected if not r.scored]
        already = len(selected) - len(todo)
        if not todo:
            messagebox.showinfo(
                "Nothing to score",
                f"All {len(selected)} selected video(s) already have "
                "results. Pick a folder/selection with unscored videos, "
                "or delete their .json sidecars to force a re-score.",
            )
            return

        self._cancel.clear()
        self._set_controls(busy=True)
        skip_note = f" ({already} already scored, skipped)" if already else ""
        self.status_lbl.configure(
            text=f"Scoring {len(todo)} video(s){skip_note}…"
        )
        items = [r.item for r in todo]
        self._worker = threading.Thread(
            target=self._worker_run,
            args=(items, api_key),
            daemon=True,
        )
        self._worker.start()

    def _worker_run(self, items, api_key) -> None:
        def progress(done: int, total: int, r: scoring.Result) -> None:
            self._post(self._on_progress, done, total, r)

        try:
            scoring.score_items(
                items,
                api_key,
                progress_cb=progress,
                cancel_event=self._cancel,
            )
        except Exception as exc:  # pragma: no cover - safety net
            self._post(self._on_fatal, str(exc))
            return
        self._post(self._on_done)

    def _on_progress(self, done, total, r: scoring.Result) -> None:
        # Fold the fresh result into the model, then re-rank/re-render.
        row = self._rows.get(str(r.path))
        if row is not None:
            row.result = r
        self._rebuild_tree()
        self.status_lbl.configure(text=f"Scoring… {done} of {total} done")

    def _on_fatal(self, msg: str) -> None:
        self._set_controls(busy=False)
        self.status_lbl.configure(text="Failed.")
        messagebox.showerror("Scoring failed", msg)

    def _on_done(self) -> None:
        self._set_controls(busy=False)
        self._rebuild_tree()

        all_results = [
            r.result for r in self._rows.values() if r.result is not None
        ]
        written = ""
        if all_results and self.folder is not None:
            try:
                jp, cp, mp = scoring.write_reports(
                    self.folder, all_results
                )
                written = f"  Reports: {jp.name}, {cp.name}, {mp.name}"
            except OSError as exc:
                written = f"  (report write failed: {exc})"

        ordered = [
            r.result
            for r in self._ordered_rows()
            if r.result is not None and r.result.ok
        ]
        ok = len(ordered)
        err = sum(
            1
            for r in self._rows.values()
            if r.result is not None and r.result.status == "error"
        )
        podium = "  ·  ".join(
            f"{MEDALS[i + 1]} {res.name} ({_num(res.frame_mean)})"
            for i, res in enumerate(ordered[:3])
        )
        summary = f"Done. {ok} scored"
        if err:
            summary += f", {err} failed"
        if podium:
            summary += f".  Top: {podium}"
        self.status_lbl.configure(text=summary + written)

    def _cancel_scoring(self) -> None:
        self._cancel.set()
        self.status_lbl.configure(
            text="Cancelling… (finishing current video)"
        )


def run_gui() -> None:
    root = tk.Tk()
    ResembleScoreGUI(root)
    root.mainloop()
