"""Rich terminal interface: pick a folder, multi-select videos, score, rank."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import client, scoring
from .discovery import VideoItem, discover

console = Console()


def _print_discovered(items: Sequence[VideoItem]) -> None:
    table = Table(title="Discovered videos", header_style="bold cyan")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Group", style="magenta")
    table.add_column("File")
    for i, it in enumerate(items, 1):
        table.add_row(str(i), it.group, it.name)
    console.print(table)


def _parse_selection(
    raw: str, items: Sequence[VideoItem]
) -> list[VideoItem]:
    """Resolve a selection token into a concrete item list.

    Accepts: ``all``; ``g:oldcam`` / ``g:original``; comma/space-separated
    1-based indices and ``a-b`` ranges (e.g. ``1,3,5-7``).
    """
    raw = raw.strip().lower()
    if not raw:
        return []
    if raw == "all":
        return list(items)
    if raw in ("g:oldcam", "oldcam"):
        return [it for it in items if it.version is not None]
    if raw in ("g:original", "original", "g:kling", "kling"):
        return [it for it in items if it.version is None]

    chosen: list[VideoItem] = []
    seen: set[int] = set()
    for tok in raw.replace(",", " ").split():
        if "-" in tok:
            a, _, b = tok.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            rng = range(min(lo, hi), max(lo, hi) + 1)
        else:
            try:
                rng = range(int(tok), int(tok) + 1)
            except ValueError:
                continue
        for n in rng:
            idx = n - 1
            if 0 <= idx < len(items) and idx not in seen:
                seen.add(idx)
                chosen.append(items[idx])
    return chosen


def _print_ranked(results: Sequence[scoring.Result]) -> None:
    ordered = scoring.rank(results)
    table = Table(
        title="Results — lower score = more authentic (winner ★)",
        header_style="bold cyan",
    )
    table.add_column("Rank", justify="right")
    table.add_column("Group", style="magenta")
    table.add_column("File")
    table.add_column("Score", justify="right")
    table.add_column("Certainty", justify="right")
    table.add_column("Status")
    for r in ordered:
        is_winner = r.rank == 1 and r.ok
        rank_txt = "★ 1" if is_winner else ("" if r.rank is None else str(r.rank))
        score_txt = "-" if r.score is None else f"{r.score:.4f}"
        cert_txt = "-" if r.certainty is None else f"{r.certainty:.4f}"
        style = (
            "bold green"
            if is_winner
            else ("red" if r.status == "error" else "")
        )
        table.add_row(
            rank_txt,
            r.group,
            r.name,
            score_txt,
            cert_txt,
            r.status if r.status == "ok" else f"[red]{r.status}[/red]"
            if r.status == "error"
            else r.status,
            style=style or None,
        )
    console.print(table)


def run_cli(
    *,
    folder: Optional[str] = None,
    recursive: bool = True,
    select_all: bool = False,
    select: Optional[str] = None,
) -> int:
    """CLI entry. Returns a process exit code (0 ok, 1 errors, 2 usage)."""
    try:
        api_key = client.resolve_api_key()
    except RuntimeError as e:
        console.print(Panel(str(e), title="Missing API key", style="red"))
        return 2

    if folder:
        root = Path(folder)
    else:
        from tk_dialogs import select_directory_cli_safe

        picked = select_directory_cli_safe(title="Select a folder of videos")
        if not picked:
            console.print("[yellow]No folder selected.[/yellow]")
            return 2
        root = Path(picked)

    try:
        items = discover(root, recursive=recursive)
    except (NotADirectoryError, FileNotFoundError) as e:
        console.print(f"[red]{e}[/red]")
        return 2

    if not items:
        console.print(
            f"[yellow]No videos found in {root}"
            f"{' (recursive)' if recursive else ''}.[/yellow]"
        )
        return 0

    _print_discovered(items)

    if select_all:
        chosen = list(items)
    elif select:
        chosen = _parse_selection(select, items)
    else:
        console.print(
            'Select videos to score — e.g. "1,3,5-7", "g:oldcam", '
            '"g:original", "all".'
        )
        try:
            raw = console.input("[cyan]Selection> [/cyan]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled.[/yellow]")
            return 2
        chosen = _parse_selection(raw, items)

    if not chosen:
        console.print("[yellow]Nothing selected.[/yellow]")
        return 2

    console.print(
        f"Scoring [bold]{len(chosen)}[/bold] video(s) sequentially...\n"
    )

    cancel_event = threading.Event()

    def progress(done: int, total: int, r: scoring.Result) -> None:
        if r.status == "ok":
            console.print(
                f"  [{done}/{total}] [green]✓[/green] {r.name}  "
                f"score={r.score:.4f} certainty="
                f"{('-' if r.certainty is None else f'{r.certainty:.4f}')}"
            )
        elif r.status == "cancelled":
            console.print(
                f"  [{done}/{total}] [yellow]…[/yellow] {r.name}  cancelled"
            )
        else:
            console.print(
                f"  [{done}/{total}] [red]✗[/red] {r.name}  {r.error}"
            )

    # score_items handles KeyboardInterrupt internally and returns the
    # partial results, so a Ctrl-C still yields a ranked, written report.
    results = scoring.score_items(
        chosen,
        api_key,
        progress_cb=progress,
        cancel_event=cancel_event,
    )
    interrupted = any(r.status == "cancelled" for r in results)
    if interrupted:
        console.print(
            "\n[yellow]Interrupted — writing report for completed "
            "videos.[/yellow]"
        )

    if not results:
        return 1

    console.print()
    _print_ranked(results)

    try:
        json_path, csv_path = scoring.write_reports(root, results)
        console.print(f"\nReports written:\n  {json_path}\n  {csv_path}")
    except OSError as e:
        console.print(
            f"\n[red]Could not write reports to {root}: {e}[/red]"
        )
        return 1

    errored = any(r.status == "error" for r in results)
    return 1 if (errored or interrupted) else 0
