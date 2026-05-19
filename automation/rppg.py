"""rPPG injection post-process — the LAST stage of the pipeline.

Pipeline order is Kling -> Loop -> Oldcam -> **rPPG**. rPPG runs last on
purpose: loop's ping-pong reverse would play a pre-injected pulse backwards
(non-physiological, detectable) and oldcam's resolution-crush would attenuate
a pre-injected sub-perceptual pulse. Injecting on the final delivered pixels
preserves the correct physiological signal.

This module shells out to the gitignored ``rPPG/`` tool via its Windows
``run_rppg.bat`` launcher. The injector itself (``rPPG/rppg_injector.py``) is
never imported or copied into tracked code. The step degrades gracefully:
if the launcher is absent, the injector errors, or it produces no output, we
log a warning and return ``None`` — callers keep the pre-rPPG video and the
run continues. It must never raise into the queue/pipeline.

Invocation (one-shot, deterministic naming):
    rPPG/run_rppg.bat "<abs in.mp4>" --inject --output "<abs out.mp4>" --skip-kinematic-gate

``--skip-kinematic-gate`` is passed deliberately: the injector's v8 kinematic
preflight is marked "new, untested" by the tool's own README. Re-enabling that
gate is a deliberate FUTURE ENHANCEMENT, not an oversight — see
docs/rppg-wiring.md.

Platform note: ``run_rppg.bat`` is Windows-only. A macOS injector launcher is
explicitly out of scope for this pass; ``run_rppg`` simply skips gracefully
(launcher-missing path) on platforms without the .bat.
"""

from __future__ import annotations

import glob as _glob
import json
import os
import queue as _queue
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ProgressCB = Optional[Callable[[str, str], None]]


def _format_cmd_for_log(cmd: List[str]) -> str:
    """Render a command list shell-paste-safe for the current OS.

    POSIX shells parse ``shlex.join``; cmd.exe / PowerShell need MS-style
    quoting from ``subprocess.list2cmdline``. Mirrors automation/oldcam.py.
    """
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


def _report(progress_cb: ProgressCB, message: str, level: str = "info") -> None:
    if progress_cb:
        progress_cb(message, level)


def resolve_rppg_launcher(repo_root: Path) -> Optional[Path]:
    """Return the rPPG launcher path if the gitignored tool is present.

    Returns ``None`` (caller skips gracefully) when ``rPPG/run_rppg.bat`` or
    ``rPPG/rppg_injector.py`` is missing — e.g. a release without the tool, or
    a non-Windows host (the launcher is a .bat).
    """
    launcher = repo_root / "rPPG" / "run_rppg.bat"
    injector = repo_root / "rPPG" / "rppg_injector.py"
    if not launcher.exists() or not injector.exists():
        return None
    return launcher


def build_rppg_output_path(input_path: Path) -> Path:
    """``video.mp4`` -> ``video-rppg.mp4`` (mirrors build_oldcam_output_path).

    Combined with oldcam this yields e.g.
    ``clip_looped-oldcam-v24-rppg.mp4`` because rPPG runs on oldcam's output.

    NOTE: this is the path we *request* via ``--output``. The injector
    then renames it, appending a metric suffix (see
    :func:`resolve_produced_output`). Do not assume this exact path exists
    after a run — resolve the actual produced file instead.
    """
    return input_path.with_name(f"{input_path.stem}-rppg{input_path.suffix}")


def is_rppg_artifact(path: Path) -> bool:
    """True if *path* has ALREADY had rPPG injected at any point in its
    processing chain.

    The injector always writes the literal token ``-rppg`` immediately
    after the input stem, then optionally renames to
    ``{stem}-rppg - <metrics>{ext}``. After that the file may be further
    processed, so the ``-rppg`` token can be:

      * at the end of the stem            ``clip-rppg``
      * the metric-rename form            ``clip-rppg - 7.8-...``
      * an INFIX when a later stage ran   ``clip-rppg-oldcam-v24``
        (rPPG was applied BEFORE oldcam; the original injection survives
        oldcam's re-encode, so re-injecting would compound the pulse out
        of the non-negotiable sub-perceptual range — Codex P2, PR #39)

    So match ``-rppg`` only as a COMPLETE token: followed by end-of-stem,
    a space (metric rename), or a hyphen (a later processing stage). The
    predicate is intentionally conservative — it errs toward "already
    injected". A rare false-positive on a human-named file (e.g.
    ``my-rppg-notes``) merely returns that file as-is without injecting,
    which is harmless; a false-NEGATIVE double-injects and breaks the
    non-negotiable sub-perceptual guarantee, so the asymmetry is
    deliberate.
    """
    stem = Path(path).stem.lower()
    return bool(re.search(r"-rppg(?:$| |-)", stem))


def resolve_produced_output(requested: Path) -> Optional[Path]:
    """Find the file the injector actually produced.

    Empirically (verified via oldcam-testing/rppg_harness.py against the
    real tool) the injector takes our ``--output`` of ``{stem}-rppg{ext}``
    and *renames* it to ``{stem}-rppg - <snr>-<phase>-<temporal>-<motion>
    -<harmonic>{ext}`` regardless of ``--output`` — the documented
    deterministic-path contract does NOT hold. So accept either the exact
    requested path or the metric-suffixed sibling, whichever is newest.
    Returns None if nothing matching was produced (graceful-skip caller).
    """
    stem = requested.stem  # e.g. "<clip>-rppg"
    ext = requested.suffix
    parent = requested.parent
    if not parent.is_dir():
        return requested if requested.exists() else None
    # The injector's rename is specifically "<stem> - <metrics><ext>" with a
    # space-hyphen-space separator (rPPG/rppg_injector.py add_metric_suffix).
    # Match ONLY that exact form (not a loose "<stem>*<ext>" which would also
    # catch the input itself or unrelated "<stem>-foo<ext>" siblings on a
    # re-run). Newest wins — INCLUDING the exact requested path in the
    # ranking: on a rerun a STALE old "<stem>{ext}" can still be on disk
    # while the injector just produced a fresh "<stem> - <metrics>{ext}";
    # an early "return requested" would hand back the stale file instead
    # of the new injection (Codex P2, PR #39). So rank exact + renamed
    # siblings together by mtime.
    #
    # glob.escape() the literal parts: real Kling/oldcam stems can contain
    # glob metacharacters — e.g. "clip (1)-oldcam-v24-rppg" or
    # "selfie[final]-rppg". Unescaped, Path.glob() treats "[..]" as a
    # character class and the produced file is silently missed, defeating
    # rPPG output detection (false graceful-skip on a successful inject).
    pattern = f"{_glob.escape(stem)} - *{_glob.escape(ext)}"
    pool = {p for p in parent.glob(pattern) if p.is_file()}
    if requested.exists() and requested.is_file():
        pool.add(requested)
    candidates = sorted(
        pool,
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


# The 5 metrics the injector embeds, in the exact order
# rPPG/rppg_injector.py::format_metric_suffix writes them
# ("{snr:.2f}-{phase:.1f}-{temporal:.2f}-{motion:.2f}-{harmonic:.2f}",
# joined to the clean stem with a literal " - " separator).
_METRIC_KEYS: Tuple[str, ...] = ("snr", "phase", "temporal", "motion", "harmonic")


def parse_metric_suffix(produced_stem: str, requested_stem: str) -> Optional[Dict[str, float]]:
    """Extract the 5 rPPG metrics the injector embedded in *produced_stem*.

    *produced_stem* is the stem of the file the injector actually wrote
    (``{requested_stem} - <snr>-<phase>-<temporal>-<motion>-<harmonic>``);
    *requested_stem* is the clean ``{...}-rppg`` stem we asked for via
    ``--output``. Returns the metrics keyed by name, or ``None`` if
    *produced_stem* is not the metric-renamed form of *requested_stem*
    (e.g. the injector honoured ``--output`` for once, or the names are
    unrelated) — callers then skip the sidecar/rename gracefully.

    The injector formats the suffix as
    ``f"{snr:.2f}-{phase:.1f}-{temporal:.2f}-{motion:.2f}-{harmonic:.2f}"``
    (rPPG/rppg_injector.py::format_metric_suffix) — exactly 5 numbers
    joined by a single ``-``. ``phase`` can be NEGATIVE, so a value's
    own leading ``-`` collides with the ``-`` separator to make ``--``.
    A greedy signed-number regex scan therefore mis-reads every
    separator as a sign. Instead split on ``-``: an EMPTY token (the
    gap inside ``--``)
    means the next number is negative. Reassemble exactly 5 values.
    """
    if produced_stem == requested_stem:
        return None
    if not produced_stem.startswith(f"{requested_stem} - "):
        return None
    tail = produced_stem[len(requested_stem) + 3:]  # drop "<stem> - "
    parts = tail.split("-")
    values: List[float] = []
    i = 0
    while i < len(parts):
        tok = parts[i]
        if tok == "":
            # Empty token => this position was a '--': the separator '-'
            # followed by a negative value's leading '-'. The number is
            # the NEXT part, negated.
            i += 1
            if i >= len(parts):
                return None
            tok = "-" + parts[i]
        try:
            values.append(float(tok))
        except ValueError:
            return None
        i += 1
    if len(values) != len(_METRIC_KEYS):
        return None
    return dict(zip(_METRIC_KEYS, values))


def finalize_rppg_output(
    produced: Path,
    requested: Path,
    *,
    keep_metrics: bool,
    progress_cb: ProgressCB = None,
) -> Path:
    """Apply the user's metric-in-filename preference to a finished inject.

    The injector ignores ``--output`` and renames its result to
    ``{stem}-rppg - <SNR>-<Phase>-<Temporal>-<Motion>-<Harmonic>{ext}``.

    * ``keep_metrics=True``  — leave that name as-is (return *produced*).
    * ``keep_metrics=False`` — rename *produced* back to the clean
      *requested* path (``{stem}-rppg{ext}``) and drop the 5 metrics into
      a ``{stem}-rppg.metrics.json`` sidecar next to it, so the numbers
      are preserved without polluting the filename.

    Single source of truth — the GUI queue and the automation pipeline
    both call this so the behaviour can't drift. Never raises: any rename
    / sidecar failure logs a warning and returns the best path we have
    (the run already succeeded; a cosmetic-rename hiccup must not lose
    the delivered video).
    """
    produced = Path(produced)
    requested = Path(requested)
    if keep_metrics:
        return produced
    if produced == requested:
        return produced  # injector honoured --output; nothing to strip

    metrics = parse_metric_suffix(produced.stem, requested.stem)
    if metrics is None:
        # produced is NOT the injector's "<requested> - <metrics>"
        # rename (malformed/unexpected injector output, or an unrelated
        # sibling). Renaming it onto ``requested`` would fabricate a
        # clean-name artifact with no sidecar AND could clobber a prior
        # legitimate clean file. Keep ``produced`` untouched — it is
        # still the delivered video, just under its own name.
        # (CodeRabbit Major, PR #40.)
        _report(
            progress_cb,
            f"rPPG: {produced.name} is not the expected metric-rename "
            f"form; keeping it as-is (no sidecar, no rename).",
            "warning",
        )
        return produced
    # metrics is guaranteed non-None here (we returned early above
    # otherwise). Write the sidecar BEFORE renaming so a rename failure
    # still leaves the metrics recorded. Keyed off the *requested*
    # (clean) stem.
    sidecar = requested.with_name(f"{requested.stem}.metrics.json")
    try:
        sidecar.write_text(
            json.dumps(
                {
                    "source": produced.name,
                    "metrics": metrics,
                    "order": list(_METRIC_KEYS),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        _report(
            progress_cb,
            f"rPPG: could not write metrics sidecar ({exc}); continuing.",
            "warning",
        )

    try:
        # A stale clean file from a previous run would block the rename
        # on Windows (os.replace overwrites, Path.rename does not — use
        # os.replace for atomic same-dir overwrite).
        os.replace(produced, requested)
    except OSError as exc:
        _report(
            progress_cb,
            f"rPPG: could not strip metric suffix ({exc}); "
            f"keeping {produced.name}.",
            "warning",
        )
        return produced
    _report(
        progress_cb,
        f"rPPG: metrics moved to sidecar; clean output {requested.name}",
        "info",
    )
    return requested


def stream_subprocess_with_timeout(
    cmd: List[str],
    *,
    cwd: str,
    timeout_seconds: int,
    on_line: Optional[Callable[[str], None]] = None,
) -> Tuple[int, List[str]]:
    """Run *cmd*, stream stdout line-by-line, and enforce a HARD wall-clock
    timeout that fires even if the child stalls mid-line with no newline
    and no EOF.

    A bare ``readline()`` loop (even with a deadline checked before each
    call) cannot honour the timeout: ``readline()`` itself blocks until a
    newline or EOF, so a silently-hung child wedges the loop forever and
    the documented graceful-skip never happens. We drain stdout on a
    daemon reader thread and let the MAIN thread own the wall clock, so a
    no-output stall is still killed on schedule.

    Returns ``(returncode, output_lines)``. Raises
    ``subprocess.TimeoutExpired`` on timeout (caller treats that as a
    graceful skip). The single source of truth for rPPG subprocess
    streaming — both automation/rppg.py and the GUI queue use it so the
    timeout behaviour can't drift between paths.
    """
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    line_q: "_queue.Queue[Optional[str]]" = _queue.Queue()

    def _drain() -> None:
        try:
            for line in process.stdout:  # type: ignore[union-attr]
                line_q.put(line)
        except Exception:
            pass
        finally:
            line_q.put(None)  # sentinel: stdout closed (process exiting)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    output_lines: List[str] = []
    deadline = time.monotonic() + timeout_seconds
    eof = False
    while not eof:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if process.poll() is None:
                process.kill()
                # Reap the SIGKILL'd child so it does not linger as a
                # zombie (kill() terminates but does not wait()). Bounded
                # so a wedged kill can't itself hang the caller.
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise subprocess.TimeoutExpired(cmd, timeout_seconds)
        try:
            item = line_q.get(timeout=min(remaining, 1.0))
        except _queue.Empty:
            continue  # re-check the wall clock; child may be silent
        if item is None:
            eof = True
            break
        text = item.rstrip()
        if text:
            output_lines.append(text)
            if on_line is not None:
                on_line(text)

    # stdout drained; the process should exit imminently. Bound the final
    # wait by whatever wall-clock budget is left.
    try:
        returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)  # reap the killed child (no zombie)
            except subprocess.TimeoutExpired:
                pass
        raise
    return returncode, output_lines


def run_rppg(
    *,
    video_path: Path,
    repo_root: Path,
    progress_cb: ProgressCB = None,
    timeout_seconds: int = 600,
    keep_metrics: bool = False,
) -> Optional[Path]:
    """Run one-shot rPPG injection. Return the output Path, or None on any
    failure (graceful skip — caller keeps the pre-rPPG video).

    *keep_metrics* selects the delivered filename: ``True`` keeps the
    injector's ``{stem}-rppg - <metrics>{ext}``; ``False`` (default)
    strips it back to a clean ``{stem}-rppg{ext}`` and writes a
    ``.metrics.json`` sidecar (see :func:`finalize_rppg_output`).
    """
    launcher = resolve_rppg_launcher(repo_root)
    if launcher is None:
        _report(progress_cb, "rPPG skipped: rPPG/ tool not present.", "warning")
        return None

    # Absolutize BEFORE building the command: the subprocess runs with
    # cwd=launcher.parent (rPPG/), so a relative video_path/--output
    # would resolve against rPPG/ instead of the caller's directory —
    # the injector would not find the input and would write the output
    # to the wrong place (CodeRabbit major, PR #39). Callers may pass a
    # relative path depending on the automation root.
    input_path = Path(video_path).resolve()
    if not input_path.exists():
        _report(progress_cb, f"rPPG skipped: input missing ({input_path.name}).", "warning")
        return None

    output_path = build_rppg_output_path(input_path)
    cmd = [
        str(launcher),
        str(input_path),
        "--inject",
        "--output",
        str(output_path),
        # Deliberate: v8 kinematic preflight is README-marked untested.
        # Re-enabling is a future enhancement (docs/rppg-wiring.md).
        "--skip-kinematic-gate",
    ]
    _report(progress_cb, f"rPPG launching: {_format_cmd_for_log(cmd)}", "info")

    output_lines: List[str] = []
    try:
        completed_returncode, output_lines = stream_subprocess_with_timeout(
            cmd,
            cwd=str(launcher.parent),
            timeout_seconds=timeout_seconds,
            on_line=lambda text: _report(progress_cb, text, "info"),
        )
    except subprocess.TimeoutExpired:
        _report(progress_cb, f"rPPG timed out after {timeout_seconds}s", "warning")
        return None
    except Exception as exc:  # never raise into the pipeline
        _report(progress_cb, f"rPPG launcher error: {exc}", "warning")
        return None

    if completed_returncode != 0:
        tail = output_lines[-15:] if output_lines else []
        _report(
            progress_cb,
            f"rPPG injection failed (exit={completed_returncode}); keeping pre-rPPG video.",
            "warning",
        )
        if tail:
            for line in tail:
                _report(progress_cb, f"  {line}", "warning")
        else:
            _report(progress_cb, "  (no stdout/stderr captured)", "warning")
        return None

    produced = resolve_produced_output(output_path)
    if produced is None:
        _report(progress_cb, "rPPG ran but output missing; keeping pre-rPPG video.", "warning")
        return None
    final = finalize_rppg_output(
        produced, output_path, keep_metrics=keep_metrics, progress_cb=progress_cb
    )
    _report(progress_cb, f"rPPG injection applied: {final.name}", "success")
    return final
