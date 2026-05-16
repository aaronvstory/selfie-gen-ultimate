"""Tkinter (ttk) GUI: folder pick → grouped checkbox tree → threaded scoring.

Threading model mirrors similarity/src/gui.py: scoring runs on a daemon
worker thread; **every** widget mutation is marshalled back onto the Tk main
loop via ``root.after(0, ...)`` so Tk is never touched off-thread.
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Optional

from . import client, scoring, theme
from .discovery import VideoItem, discover

CHECK_ON = "☑"   # ☑
CHECK_OFF = "☐"  # ☐


class ResembleScoreGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("resemble-score — deepfake scoring & comparison")
        self.root.geometry("1080x720")
        theme.apply_dark_ttk(self.root)

        self.folder: Optional[Path] = None
        self.recursive = tk.BooleanVar(value=True)
        self.items: list[VideoItem] = []
        # tree item id -> (VideoItem, checked bool); group rows excluded
        self._rows: dict[str, list] = {}
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()

        self._build()

    # ---- layout -----------------------------------------------------------
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")
        ttk.Label(
            top, text="resemble-score", style="Title.TLabel"
        ).pack(side="left")
        ttk.Label(
            top,
            text="lower score = looks more authentic (winner ★)",
            style="Muted.TLabel",
        ).pack(side="left", padx=12)

        bar = ttk.Frame(self.root, padding=(10, 0))
        bar.pack(fill="x")
        self.pick_btn = ttk.Button(
            bar, text="Pick Folder…", command=self._pick_folder
        )
        self.pick_btn.pack(side="left")
        # Re-run discovery when the mode changes so the list never goes stale.
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

        cols = ("group", "score", "certainty", "status")
        self.tree = ttk.Treeview(
            self.root, columns=cols, show="tree headings", selectmode="none"
        )
        self.tree.heading("#0", text="  ☑  File")
        self.tree.heading("group", text="Group")
        self.tree.heading("score", text="Score")
        self.tree.heading("certainty", text="Certainty")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=520, anchor="w")
        self.tree.column("group", width=130, anchor="w")
        self.tree.column("score", width=100, anchor="e")
        self.tree.column("certainty", width=100, anchor="e")
        self.tree.column("status", width=110, anchor="w")
        self.tree.tag_configure(
            "winner", background=theme.WINNER_BG, foreground=theme.WINNER_FG
        )
        self.tree.tag_configure("error", foreground=theme.ERROR_FG)
        self.tree.pack(fill="both", expand=True, padx=10, pady=6)
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
        # Toggling the mode after a folder is picked must re-scan, else the
        # displayed list no longer matches the selected recursion mode.
        if self.folder is not None:
            self._start_discovery()

    def _start_discovery(self) -> None:
        """Scan the folder on a worker thread (rglob on a large tree would
        otherwise freeze the UI), then repopulate the tree via root.after."""
        if self._worker and self._worker.is_alive():
            return
        if not self.folder:
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self._rows.clear()
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
        except (NotADirectoryError, FileNotFoundError) as e:
            self.root.after(0, self._on_discovery_error, str(e))
            return
        except OSError as e:  # unexpected FS error — surface, don't crash
            self.root.after(0, self._on_discovery_error, str(e))
            return
        self.root.after(0, self._populate_tree, items)

    def _on_discovery_error(self, msg: str) -> None:
        self._set_controls(busy=False)
        self.status_lbl.configure(text="Discovery failed.")
        messagebox.showerror("Discovery failed", msg)

    def _populate_tree(self, items: list[VideoItem]) -> None:
        self.items = items
        self._set_controls(busy=False)
        if not items:
            self.status_lbl.configure(text="No videos found in this folder.")
            return

        groups: dict[str, str] = {}
        for it in items:
            if it.group not in groups:
                gid = self.tree.insert(
                    "", "end", text=it.group, values=("", "", "", ""),
                    open=True,
                )
                groups[it.group] = gid
            row = self.tree.insert(
                groups[it.group],
                "end",
                text=f"  {CHECK_ON}  {it.name}",
                values=(it.group, "", "", ""),
            )
            self._rows[row] = [it, True]
        self.status_lbl.configure(
            text=f"{len(items)} video(s) found. Pick which to score."
        )

    # ---- checkbox handling ------------------------------------------------
    def _set_check(self, row: str, checked: bool) -> None:
        item, _ = self._rows[row]
        self._rows[row] = [item, checked]
        mark = CHECK_ON if checked else CHECK_OFF
        self.tree.item(row, text=f"  {mark}  {item.name}")

    def _on_click(self, event: tk.Event) -> None:
        if self._worker and self._worker.is_alive():
            return
        row = self.tree.identify_row(event.y)
        if row in self._rows:
            _, checked = self._rows[row]
            self._set_check(row, not checked)

    def _set_all(self, checked: bool) -> None:
        for row in self._rows:
            self._set_check(row, checked)

    def _select_group(self, *, oldcam: bool) -> None:
        for row, (item, _) in list(self._rows.items()):
            self._set_check(row, (item.version is not None) == oldcam)

    def _selected_rows(self) -> list[tuple]:
        return [
            (row, item)
            for row, (item, checked) in self._rows.items()
            if checked
        ]

    # ---- scoring (threaded) ----------------------------------------------
    def _set_controls(self, *, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for btn in (
            self.pick_btn,
            self.all_btn,
            self.none_btn,
            self.oldcam_btn,
            self.orig_btn,
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

        selected = self._selected_rows()
        if not selected:
            messagebox.showinfo(
                "Nothing selected", "Tick at least one video to score."
            )
            return

        # Clear stale winner/error styling from ALL rows (a prior winner may
        # be outside this selection and must not stay starred/highlighted).
        for row, (item, checked) in self._rows.items():
            mark = CHECK_ON if checked else CHECK_OFF
            self.tree.item(row, text=f"  {mark}  {item.name}", tags=())
        # Reset result columns for the selected rows.
        for row, item in selected:
            self.tree.item(
                row, values=(item.group, "", "", "queued"), tags=()
            )

        self._cancel.clear()
        self._set_controls(busy=True)
        row_by_path = {
            str(item.path): row for row, item in selected
        }
        items = [item for _, item in selected]

        self._worker = threading.Thread(
            target=self._worker_run,
            args=(items, api_key, row_by_path),
            daemon=True,
        )
        self._worker.start()

    def _worker_run(self, items, api_key, row_by_path) -> None:
        def progress(done: int, total: int, r: scoring.Result) -> None:
            self.root.after(
                0, self._on_progress, done, total, r, row_by_path
            )

        try:
            results = scoring.score_items(
                items,
                api_key,
                progress_cb=progress,
                cancel_event=self._cancel,
            )
        except Exception as exc:  # pragma: no cover - safety net
            self.root.after(0, self._on_fatal, str(exc))
            return
        self.root.after(0, self._on_done, results)

    def _on_progress(self, done, total, r, row_by_path) -> None:
        row = row_by_path.get(str(r.path))
        if row is not None:
            score_txt = "-" if r.score is None else f"{r.score:.4f}"
            cert_txt = "-" if r.certainty is None else f"{r.certainty:.4f}"
            tags = ("error",) if r.status == "error" else ()
            self.tree.item(
                row,
                values=(r.group, score_txt, cert_txt, r.status),
                tags=tags,
            )
        self.status_lbl.configure(text=f"Scoring… {done} of {total} done")

    def _on_fatal(self, msg: str) -> None:
        self._set_controls(busy=False)
        self.status_lbl.configure(text="Failed.")
        messagebox.showerror("Scoring failed", msg)

    def _on_done(self, results) -> None:
        self._set_controls(busy=False)
        ordered = scoring.rank(results)
        winner = next((x for x in ordered if x.ok), None)
        if winner is not None:
            for row, (item, _) in self._rows.items():
                if str(item.path) == str(winner.path):
                    vals = self.tree.item(row, "values")
                    self.tree.item(row, values=vals, tags=("winner",))
                    self.tree.item(
                        row, text=f"  ★  {item.name}"
                    )
                    break

        try:
            json_path, csv_path = scoring.write_reports(
                self.folder, results
            )
            written = f"  Reports: {json_path.name}, {csv_path.name}"
        except OSError as exc:
            written = f"  (report write failed: {exc})"

        ok = sum(1 for x in results if x.status == "ok")
        err = sum(1 for x in results if x.status == "error")
        cancelled = sum(1 for x in results if x.status == "cancelled")
        summary = f"Done. {ok} scored"
        if err:
            summary += f", {err} failed"
        if cancelled:
            summary += f", {cancelled} cancelled"
        self.status_lbl.configure(text=summary + "." + written)

    def _cancel_scoring(self) -> None:
        self._cancel.set()
        self.status_lbl.configure(text="Cancelling… (finishing current video)")


def run_gui() -> None:
    root = tk.Tk()
    ResembleScoreGUI(root)
    root.mainloop()
