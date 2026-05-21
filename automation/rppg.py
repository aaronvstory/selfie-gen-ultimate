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


# rPPG iterative-mode progress markers. The injector prints these to
# stdout (verified via Explore agent recon, PR #43):
#   "  Iteration N/M" — per-iter header (line ~3758 in rppg_injector.py)
#   "  All targets met at iteration N!" — early convergence success
#   "  Stopping to avoid over-processing" — plateau-stop exit
#   "GPU backend: CuPy X.Y on N device(s)" — GPU detected
#   "GPU backend: CuPy unavailable" — CPU fallback
# These regexes are matched on .strip()'d lines so leading whitespace
# variations don't break the parse.
_RPPG_ITER_RE = re.compile(r"^Iteration\s+(\d+)\s*/\s*(\d+)\b")
_RPPG_DONE_RE = re.compile(
    r"^(All targets met at iteration|Stopping to avoid over-processing|"
    r"Best iteration|Plateau stop|Converged)"
)
# User-requested surfacing (PR #43): elevate the "GPU detected" /
# "GPU unavailable" injector line to info-level even when verbose is
# off, so the user can immediately see whether their RTX 4090 was
# actually used (Windows) or whether the M1 Pro fell back to CPU
# (CuPy doesn't support Metal — out of scope for now).
_RPPG_GPU_RE = re.compile(r"^GPU backend:\s*(.+)$")


class _RppgProgressTracker:
    """Stateful stdout parser for the rPPG injector's iterative mode.

    Two jobs:
      * Emit user-friendly progress lines via the host's report-cb
        (e.g. "rPPG iteration 3/10 (~30%)") so the GUI matches the
        oldcam progress pattern. Raw stdout is reserved for verbose
        mode — when verbose=False the raw lines are downgraded to
        "debug" level (which the GUI silently drops) and only the
        synthesized progress lines surface at "info".
      * Provide a deadline_extender that ratchets the subprocess wall
        clock forward by ~90s every time a new iteration starts.
        Combined with the bumped initial timeout, this means a
        well-behaved iterative run NEVER hits an "arbitrary" wall —
        the wall only kicks in when the injector goes silent (real
        stall / wedge). Friend feedback to PR #43.

    Per-iteration bump is generous (90s vs the ~60s realistic per-iter
    typical) because some iters spend more time on encode than analyze
    and we'd rather over-grant than kill a healthy run.
    """

    _PER_ITER_BUMP_SECONDS = 90

    def __init__(
        self,
        *,
        report_cb: Optional["ProgressCB"] = None,
        verbose: bool = False,
    ) -> None:
        self._report_cb = report_cb
        self._verbose = verbose
        self._iter_current: Optional[int] = None
        self._iter_max: Optional[int] = None

    def deadline_extender(self, line: str) -> int:
        """Return extra seconds to add to the deadline for *line*.

        Returns POSITIVE only when this line marks a NEW iteration
        starting (one we haven't seen before). Other lines return 0
        so the wall clock proceeds normally; if the injector goes
        silent the deadline eventually fires (graceful skip).

        Self-stateful: when this returns positive, ``_iter_current``
        advances internally so subsequent calls with the SAME iter
        return 0. The streamer calls THIS extender FIRST and ``on_line``
        SECOND (PR #43 / CodeRabbit + Codex caught the inverted order
        in 91af11f — when on_line ran first it updated _iter_current
        and the extender then saw "already seen" and returned 0,
        defeating the adaptive timeout). Self-statefulness is kept as
        defense-in-depth for tests + future callers that may exercise
        the extender independently of on_line."""
        m = _RPPG_ITER_RE.match(line.strip())
        if m is None:
            return 0
        try:
            cur = int(m.group(1))
        except (TypeError, ValueError):
            return 0
        # Only extend when a NEW iteration starts (cur changed). The
        # injector emits the same "Iteration N/M" line once per iter
        # so this guard rarely fires extra, but a future change that
        # emits sub-lines containing the iter marker can't trick us.
        if self._iter_current is not None and cur <= self._iter_current:
            return 0
        # Advance internal state so the NEXT call (same or lower iter)
        # returns 0 without needing on_line() to fire in between.
        self._iter_current = cur
        return self._PER_ITER_BUMP_SECONDS

    def on_line(self, line: str) -> None:
        """Called for every non-empty subprocess stdout line. Updates
        internal iteration state and emits user-friendly progress.

        Verbose mode (``automation_verbose_logging=True``): EVERY line
        is reported at "info" so the user sees the full injector
        chatter. Non-verbose: only the synthesized progress lines fire
        at "info"; raw injector chatter goes to "debug" (silently
        dropped by GUI loggers but kept in the file log)."""
        stripped = line.strip()

        # GPU detection — always surface this at info, regardless of
        # verbose. User asked: "look into if we can have it utilize
        # our RTX 4090". The injector auto-detects CuPy + CUDA at
        # import and prints one line either way; we want the user to
        # see that line so they know whether their GPU is in use.
        # CuPy is CUDA-only — Apple Silicon (M1 Pro) always falls
        # back to CPU here; a Metal port would be a separate task.
        m_gpu = _RPPG_GPU_RE.match(stripped)
        if m_gpu is not None:
            _report(
                self._report_cb,
                f"rPPG backend — {m_gpu.group(1)}",
                "info",
            )
            if self._verbose:
                _report(self._report_cb, line, "info")
            return

        m_iter = _RPPG_ITER_RE.match(stripped)
        if m_iter is not None:
            try:
                cur = int(m_iter.group(1))
                total = int(m_iter.group(2))
            except (TypeError, ValueError):
                pass
            else:
                self._iter_current = cur
                self._iter_max = total
                pct = int((cur / max(total, 1)) * 100)
                _report(
                    self._report_cb,
                    f"rPPG iteration {cur}/{total} (~{pct}%)",
                    "info",
                )
                # The raw injector line itself is debug-level when
                # not verbose — we've already surfaced the friendly
                # progress version above.
                if self._verbose:
                    _report(self._report_cb, line, "info")
                return

        m_done = _RPPG_DONE_RE.match(stripped)
        if m_done is not None:
            at = self._iter_current
            label = stripped if len(stripped) < 80 else stripped[:77] + "..."
            if at is not None:
                _report(
                    self._report_cb,
                    f"rPPG converged at iteration {at}: {label}",
                    "info",
                )
            else:
                _report(self._report_cb, f"rPPG: {label}", "info")
            if self._verbose:
                _report(self._report_cb, line, "info")
            return

        # All other lines: verbose -> info, non-verbose -> debug.
        _report(
            self._report_cb, line, "info" if self._verbose else "debug",
        )


def stream_subprocess_with_timeout(
    cmd: List[str],
    *,
    cwd: str,
    timeout_seconds: int,
    on_line: Optional[Callable[[str], None]] = None,
    deadline_extender: Optional[Callable[[str], int]] = None,
) -> Tuple[int, List[str]]:
    """Run *cmd*, stream stdout line-by-line, and enforce a wall-clock
    timeout that fires even if the child stalls mid-line with no newline
    and no EOF.

    A bare ``readline()`` loop (even with a deadline checked before each
    call) cannot honour the timeout: ``readline()`` itself blocks until a
    newline or EOF, so a silently-hung child wedges the loop forever and
    the documented graceful-skip never happens. We drain stdout on a
    daemon reader thread and let the MAIN thread own the wall clock, so a
    no-output stall is still killed on schedule.

    *deadline_extender* (PR #43, friend feedback "no arbitrary timeout"):
    optional callback invoked with each non-empty line; if it returns a
    POSITIVE int, the deadline is pushed forward by that many seconds.
    Used by rPPG iterative mode to ratchet the wall clock forward as
    long as the injector keeps emitting progress (Iteration N/M lines)
    — so a legitimate 10-iteration run isn't killed mid-iter just
    because the user picked a low initial timeout.

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
    start_time = time.monotonic()
    deadline = start_time + timeout_seconds
    # Hard cap on cumulative extensions — without this, a stuck
    # subprocess that emits the same "Iteration N/M" marker in a
    # loop could push the deadline out indefinitely. 8× the initial
    # timeout matches the worst real-world case (heavily iterative
    # rPPG run with 8 retries × per-iter timeout) and bites well
    # before the user notices the GUI is unresponsive. (Gemini
    # MEDIUM on 9d9a473.)
    max_deadline = start_time + max(timeout_seconds * 8, 600.0)
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
            # ORDER MATTERS (CodeRabbit Major 3272966501 + Codex P1
            # 3272968645 caught the same bug on 91af11f): the
            # deadline_extender MUST run BEFORE on_line. The progress
            # tracker's on_line updates _iter_current immediately when
            # it sees an "Iteration N/M" marker; if the extender ran
            # AFTER, it would see the same iter as "already seen" and
            # return 0 — defeating the entire "no arbitrary timeout"
            # contract. Extender first, on_line second.
            if deadline_extender is not None:
                try:
                    extra = deadline_extender(text)
                    if extra and extra > 0:
                        # Accumulate per-iteration budget. For a
                        # well-behaved run the deadline grows
                        # ``deadline + extra``. For a slow run where
                        # the deadline has already drifted close to
                        # ``now``, the previous progress marker buys
                        # at least ``now + extra`` of headroom — the
                        # ``max(deadline, now + extra)`` floor —
                        # otherwise the next iteration could die
                        # immediately despite explicit progress.
                        # Both branches are then capped at
                        # max_deadline so a buggy stuck subprocess
                        # can't push the deadline forever.
                        # (Gemini MEDIUM on 0f5c5f3.)
                        deadline = min(
                            max(deadline + extra, time.monotonic() + extra),
                            max_deadline,
                        )
                except Exception:
                    # Never let an extender bug kill the subprocess wait.
                    pass
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
    timeout_seconds: Optional[int] = None,
    keep_metrics: bool = False,
    iterative: bool = True,
    iterate_from_baseline: bool = True,
    skip_diagnosis: bool = True,
    skip_kinematic_gate: bool = True,
    verbose: bool = False,
) -> Optional[Path]:
    """Run rPPG injection. Return the output Path, or None on any
    failure (graceful skip — caller keeps the pre-rPPG video).

    *iterative* (default ``True``): re-inject with PID-adjusted
    settings until the score converges. The friend who wrote the
    injector confirmed iterative is MANDATORY for production: the
    initial single-shot injection rarely lands at the optimal strength
    and iterative tunes via feedback. Matches ``rPPG/rppg.bat`` which
    passes ``--iterative`` unconditionally. Set ``False`` only for
    back-to-back calibration / A-B testing against a fixed-param run.

    *iterate_from_baseline* (default ``True``, ignored when not
    iterative): each iteration re-injects from the ORIGINAL input,
    not the previous iteration's output. Avoids cumulative encoding
    loss and gives the PID clean slope estimates per iter. Matches
    the launcher.

    *skip_diagnosis* (default ``True``): bypass the automatic Claude-
    API diagnosis that runs after iterative injection. Diagnosis
    requires ``ANTHROPIC_API_KEY`` and costs API spend; the friend's
    .bat skips it. Set ``False`` only when you want the post-run
    diagnostic writeup.

    *skip_kinematic_gate* (default ``True``): bypass the v8 facial-
    kinematic preflight. Per docs/rppg-wiring.md the gate is README-
    marked untested.

    *keep_metrics* selects the delivered filename: ``True`` keeps the
    injector's ``{stem}-rppg - <metrics>{ext}``; ``False`` (default)
    strips it back to a clean ``{stem}-rppg{ext}`` and writes a
    ``.metrics.json`` sidecar (see :func:`finalize_rppg_output`).

    *timeout_seconds* (default ``None`` → 1800 / 30 min for iterative,
    600 / 10 min for one-shot): wall-clock initial deadline. In
    iterative mode the deadline is RATCHETED FORWARD by ~90s every
    time a new "Iteration N/M" line lands on stdout — friend feedback
    ("hope you're not still going to use some arbitrary timeout").
    The wall only fires on a silent injector (real wedge), never on a
    legitimate long run that keeps emitting progress markers. Pass
    explicit int to override (use 0 to disable extension and pin the
    deadline at the literal value, useful for tests).

    *verbose* (default ``False``): when True, every line of the
    injector's stdout is reported at "info" level. Off, only the
    synthesized per-iteration progress lines surface at "info" and the
    raw chatter goes to "debug" (silently dropped by GUI loggers).
    Wired to ``automation_verbose_logging`` upstream.
    """
    # Defaults depend on mode: iterative needs longer because the PID
    # runs up to 10 iterations + final encode. One-shot is bounded.
    if timeout_seconds is None:
        timeout_seconds = 1800 if iterative else 600
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
    cmd: List[str] = [
        str(launcher),
        str(input_path),
        "--inject",
        "--output",
        str(output_path),
    ]
    # Iterative + companion flags. ORDER mirrors rPPG/rppg.bat for
    # easy visual diff against the canonical launcher:
    #     --inject --iterative --iterate-from-base --skip-diagnosis
    # plus our --skip-kinematic-gate (preserved from before).
    if iterative:
        cmd.append("--iterative")
        if iterate_from_baseline:
            # argparse accepts the --iterate-from-base prefix (the .bat
            # form); the full flag is --iterate-from-baseline. We pass
            # the full form so a future strict-prefix injector still
            # works.
            cmd.append("--iterate-from-baseline")
        if skip_diagnosis:
            # Skip the post-iteration Claude diagnosis ("clod
            # diagnostics" per friend). Only applies after iterative
            # runs; harmless on single-shot but we still avoid adding
            # noise to the cmd when iterative is off.
            cmd.append("--skip-diagnosis")
    if skip_kinematic_gate:
        cmd.append("--skip-kinematic-gate")
    _report(progress_cb, f"rPPG launching: {_format_cmd_for_log(cmd)}", "info")

    # Progress tracker: parses Iteration N/M markers from stdout, emits
    # user-friendly progress ("rPPG iteration 3/10 (~30%)") at info,
    # downgrades raw chatter to debug unless verbose=True. In iterative
    # mode it also extends the wall-clock deadline by ~90s every time
    # a new iteration starts (friend feedback "no arbitrary timeout").
    tracker = _RppgProgressTracker(report_cb=progress_cb, verbose=verbose)
    # Codex P2 (PR #43, bot pass on 2a32f938): caller contract is that
    # timeout_seconds=0 pins a HARD deadline with no adaptive extension.
    # The prior code enabled the extender unconditionally on iterative,
    # which let strict/fast-fail callers (and tests) run for many extra
    # minutes because each Iteration line added ~90s to the deadline.
    extender = tracker.deadline_extender if (iterative and timeout_seconds and timeout_seconds > 0) else None
    output_lines: List[str] = []
    try:
        completed_returncode, output_lines = stream_subprocess_with_timeout(
            cmd,
            cwd=str(launcher.parent),
            timeout_seconds=timeout_seconds,
            on_line=tracker.on_line,
            deadline_extender=extender,
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
