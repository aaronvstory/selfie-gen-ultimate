"""
Video AA — adversarial-attack re-encode wrapper.

Optional 4th post-processing step (alongside oldcam / rPPG / crush). Applies
adversarial perturbations + synthetic capture artefacts engineered to evade
AI-generated-video / deepfake detectors. Authorized red-team / detector-research
use only.

Pipeline slot: Loop → Crush → AA → Oldcam (Phase E order). Each selected AA
attack-pipeline produces its own output file, which then fans through Oldcam
(mirrors the crush-tier fan-out).

CRITICAL — isolation: unlike rPPG/oldcam (which run off the SHARED main repo
venv), the AA tool's deps (numpy>=2, opencv>=4.10, optional torch) conflict
with the main repo invariant (numpy<2 / opencv<4.12). It therefore runs in its
OWN isolated uv venv (``aa-video/.venv``) which the AA LAUNCHER owns — this
module shells out to that launcher (``aa-video/aa_launcher.bat`` on Windows,
``aa_launcher.sh``/``.command`` elsewhere), NEVER to ``sys.executable``.

Graceful-skip contract (mirrors rPPG/crush): a missing launcher, missing
ffmpeg, or a failed run is a graceful skip (return None) — never a hard crash —
unless the caller enforces a ``required`` flag at its own layer.
"""

import os
import re
import subprocess
import threading
from pathlib import Path
from typing import List, Optional

from automation.video_loop import check_ffmpeg_available

# ---------------------------------------------------------------------------
# Selectable AA attack pipelines (2026-06-18). Mirrors the crush-resolution
# fan-out: the user ticks one or more pipelines, each producing its own output
# file. "prime" is the friend's recommended default (generic-classifier
# evasion, CPU-only). scenario1/scenario3 target specific detector families.
#
#   label -> aa-video CLI --attack name
# ---------------------------------------------------------------------------
AA_PIPELINES = {
    "prime": "prime",
    "scenario1": "scenario1",
    "scenario3": "scenario3",
}

# Fresh-install default: prime ON (the tool's own recommended pipeline).
DEFAULT_AA_ATTACKS: List[str] = ["prime"]

# Distinct stem suffix per attack so multiple AA files never collide
# (e.g. clip_aa-prime.mp4 + clip_aa-s1.mp4).
AA_SUFFIXES = {
    "prime": "_aa-prime",
    "scenario1": "_aa-s1",
    "scenario3": "_aa-s3",
}

# Display order = highest-value first (prime is the headline / back-compat
# primary, then the scenario passes).
_AA_ORDER = {"prime": 3, "scenario1": 2, "scenario3": 1}

# Sentinel distinguishing "key absent" from an explicit None/False so the
# legacy-migration branch can tell a brand-new config from one where the user
# deliberately turned AA off.
_UNSET = object()


def _canon_attack(value) -> Optional[str]:
    """Map a loose attack token to a canonical pipeline label or None.

    Accepts ``"prime"``, ``"scenario1"``/``"s1"``, ``"scenario3"``/``"s3"``.
    """
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in AA_PIPELINES:
        return token
    if token in ("s1",):
        return "scenario1"
    if token in ("s3",):
        return "scenario3"
    return None


def normalize_aa_attacks(attacks=_UNSET, legacy_enabled=_UNSET) -> List[str]:
    """Resolve the effective ordered list of AA attack-pipeline labels.

    Single source of truth shared by the GUI queue, the CLI pipeline, and the
    config panel so the legacy-key migration behaves identically everywhere.
    Mirrors :func:`automation.video_crush.normalize_crush_resolutions`.

    Precedence:
      1. ``attacks`` present (list/tuple/str) → filter to valid labels, dedup,
         order highest-first (prime → scenario1 → scenario3). An explicit empty
         list means "AA off".
      2. else fall back on the legacy boolean ``aa_enabled``:
           True  → ['prime']  (the recommended default pipeline)
           False → []         (user deliberately disabled AA; stay off)
      3. else (neither key present — a brand-new config) → DEFAULT (['prime']).

    Args:
        attacks:        The ``aa_attacks`` config value, or ``_UNSET`` when
                        the key is absent.
        legacy_enabled: The legacy ``aa_enabled`` boolean, or ``_UNSET`` when
                        that key is absent.
    """
    if attacks is not _UNSET and attacks is not None:
        if isinstance(attacks, str):
            attacks = [attacks]
        if isinstance(attacks, (list, tuple)):
            seen: set = set()
            valid: List[str] = []
            for raw in attacks:
                label = _canon_attack(raw)
                if label and label not in seen:
                    seen.add(label)
                    valid.append(label)
            return sorted(valid, key=lambda lbl: _AA_ORDER.get(lbl, 0), reverse=True)
        # Unknown type → treat as "not set".
    if legacy_enabled is not _UNSET and legacy_enabled is not None:
        return ["prime"] if bool(legacy_enabled) else []
    return list(DEFAULT_AA_ATTACKS)


def aa_suffix(label: str) -> str:
    """Return the stem suffix for an attack label (``"prime"`` → ``_aa-prime``)."""
    return AA_SUFFIXES.get(label, "_aa")


def resolve_aa_launcher(repo_root: Path) -> Optional[Path]:
    """Return the AA launcher path for the current OS.

    - Windows (``os.name == 'nt'``): ``aa-video/aa_launcher.bat``
    - Everywhere else: ``aa-video/aa_launcher.sh``

    Returns ``None`` (caller skips gracefully) when the launcher or the tool
    entrypoint (``aa-video/main.py``) is missing — e.g. a packaged build that
    excludes the subproject, or a partial clone. Mirrors
    :func:`automation.rppg.resolve_rppg_launcher`.
    """
    aa_dir = repo_root / "aa-video"
    entry = aa_dir / "main.py"
    if os.name == "nt":
        launcher = aa_dir / "aa_launcher.bat"
    else:
        launcher = aa_dir / "aa_launcher.sh"
    if not launcher.exists() or not entry.exists():
        return None
    return launcher


def check_aa_available(repo_root: Optional[Path] = None) -> tuple[bool, Optional[str]]:
    """True + None when AA can run (launcher present AND ffmpeg available).

    Returns ``(False, reason)`` otherwise so callers can log/skip. ffmpeg is
    required because every AA pipeline ends in an ffmpeg recompress step.
    """
    root = repo_root or Path(__file__).resolve().parent.parent
    if resolve_aa_launcher(root) is None:
        return False, "AA launcher or aa-video/main.py not found (subproject absent)."
    ffmpeg_ok, ffmpeg_msg = check_ffmpeg_available()
    if not ffmpeg_ok:
        return False, ffmpeg_msg
    return True, None


def build_aa_output_path(input_path: Path, attack: str, strength: float) -> Path:
    """The path we *request* via ``--output``.

    We pass an explicit ``--output`` so the produced filename is deterministic
    (``{stem}{aa_suffix}{ext}``). The tool honours ``--output``; the metric/
    collision rename only kicks in when ``--output`` is omitted, so this is the
    primary expected path — but resolve the real file via
    :func:`resolve_produced_aa_output` for belt-and-suspenders.
    """
    return input_path.with_name(f"{input_path.stem}{aa_suffix(attack)}{input_path.suffix}")


def is_aa_artifact(path: Path) -> bool:
    """True if *path* already carries an ``_aa`` token in its stem.

    Conservative (errs toward "already processed") to avoid double-applying AA
    in a re-run chain. Matches ``_aa`` as a complete token: followed by
    end-of-stem, a hyphen (``_aa-prime``), or another underscore.
    """
    stem = Path(path).stem.lower()
    return bool(re.search(r"_aa(?:$|-|_)", stem))


def resolve_produced_aa_output(
    requested: Path, attack: str, strength: float
) -> Optional[Path]:
    """Resolve the real file the tool produced.

    The aa-video tool honours ``--output`` (so ``requested`` is normally exact),
    but its no-``--output`` default writes ``{stem}_{attack}_{strength}{ext}``
    with a numbered-collision fallback (``{stem}_{attack}N_{strength}{ext}``).
    This globs both forms and returns the newest match, mirroring
    :func:`automation.rppg.resolve_produced_output`.
    """
    if requested.exists():
        return requested

    parent = requested.parent
    # The requested path's stem already includes our deterministic aa-suffix;
    # also probe the tool's own default naming as a fallback.
    candidates: List[Path] = []
    if parent.is_dir():
        # Original input stem is the requested stem minus our aa-suffix.
        suffix_token = aa_suffix(attack)
        base_stem = requested.stem
        if base_stem.endswith(suffix_token):
            base_stem = base_stem[: -len(suffix_token)]
        ext = requested.suffix.lower()
        # Tool default-naming forms: {base}_{attack}_{strength}{ext} and the
        # numbered-collision {base}_{attack}N_{strength}{ext}. Match with a
        # LITERAL prefix/suffix scan via iterdir() rather than Path.glob() —
        # a base_stem containing glob metacharacters ([, ], *, ?) would
        # mis-match or match the wrong files under glob (codex WARNING).
        prefix = f"{base_stem}_{attack}"
        try:
            for p in parent.iterdir():
                if (
                    p.is_file()
                    and p.name.startswith(prefix)
                    and p.suffix.lower() == ext
                ):
                    candidates.append(p)
        except OSError:
            candidates = []
    if not candidates:
        return None
    # Newest wins.
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _format_cmd_for_log(cmd: List[str]) -> str:
    """Shell-paste-safe command rendering (POSIX vs Windows). Mirrors oldcam.py."""
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    import shlex
    return shlex.join(cmd)


def run_aa(
    input_path: str,
    output_path: Optional[str] = None,
    attack: str = "prime",
    strength: float = 0.5,
    generator: Optional[str] = "generic",
    suffix: str = "_aa",
    log_callback=None,
    repo_root: Optional[Path] = None,
    timeout_seconds: int = 600,
) -> Optional[str]:
    """Run one AA attack-pipeline on *input_path* via the isolated launcher.

    Shells out to ``aa-video/aa_launcher.{bat,sh}`` (which owns the isolated uv
    venv) — NOT ``sys.executable``. Streams the launcher's stdout line-by-line
    through ``log_callback`` (mirrors oldcam.py's Popen streaming).

    Args:
        input_path:    Path to the input video.
        output_path:   Explicit output path. If None, derives from input stem +
                       the attack's suffix (``{stem}_aa-prime{ext}`` etc.).
        attack:        AA pipeline name (prime / scenario1 / scenario3 / …).
        strength:      Base strength 0.1–1.0 (clamped).
        generator:     Generator profile (generic/seedance/kling/runway) or None.
        suffix:        Fallback stem suffix when output_path is None and the
                       attack isn't in AA_SUFFIXES.
        log_callback:  Optional function(message: str, level: str).
        repo_root:     Repo root override (defaults to this file's grandparent).
        timeout_seconds: Hard kill after this many seconds.

    Returns:
        Absolute output path string on success, None on graceful skip / failure.
    """

    def log(msg: str, level: str = "info") -> None:
        if log_callback:
            log_callback(msg, level)

    root = repo_root or Path(__file__).resolve().parent.parent

    input_file = Path(input_path).resolve()
    if not input_file.exists() or not input_file.is_file():
        log(f"AA: input file not found: {input_path}", "error")
        return None

    launcher = resolve_aa_launcher(root)
    if launcher is None:
        log("AA: launcher/subproject not found — skipping (aa-video absent).", "warning")
        return None

    ffmpeg_ok, ffmpeg_msg = check_ffmpeg_available()
    if not ffmpeg_ok:
        log(f"AA: ffmpeg unavailable — skipping. {ffmpeg_msg}", "warning")
        return None

    # Clamp strength to the tool's accepted 0.1–1.0 range.
    try:
        strength = max(0.1, min(1.0, float(strength)))
    except (TypeError, ValueError):
        strength = 0.5

    attack_label = _canon_attack(attack) or "prime"

    if output_path is None:
        out_suffix = aa_suffix(attack_label) if attack_label in AA_SUFFIXES else suffix
        output_file = input_file.parent / f"{input_file.stem}{out_suffix}{input_file.suffix}"
    else:
        output_file = Path(output_path).resolve()

    try:
        output_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"AA: failed to create output directory: {exc}", "error")
        return None

    aa_dir = launcher.parent
    cmd: List[str] = [
        str(launcher),
        "--input", str(input_file),
        "--attack", attack_label,
        "--strength", str(strength),
        "--output", str(output_file),
    ]
    if generator:
        cmd += ["--generator", str(generator)]

    log(f"AA {attack_label} launching: {_format_cmd_for_log(cmd)}", "info")

    env = dict(os.environ)
    env["KLING_NO_PAUSE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    output_lines: List[str] = []
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(aa_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        # Watchdog: a hung/noisy child can block forever inside the
        # `for line in process.stdout` read loop, so `process.wait(timeout=)`
        # AFTER the loop would never be reached (codex CRITICAL). Arm a timer
        # that kills the process at the deadline; the read loop then sees EOF
        # and exits, and `timed_out` flags the result as a timeout.
        timed_out = {"hit": False}

        def _kill_on_timeout() -> None:
            timed_out["hit"] = True
            try:
                process.kill()
            except Exception:
                pass

        watchdog = threading.Timer(timeout_seconds, _kill_on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in process.stdout:
                line_text = line.rstrip()
                if line_text:
                    output_lines.append(line_text)
                    log(line_text, "info")
            returncode = process.wait()
        finally:
            watchdog.cancel()
        if timed_out["hit"]:
            log(f"AA {attack_label} timed out after {timeout_seconds}s", "warning")
            return None
    except Exception as exc:
        log(f"AA {attack_label} launcher error: {exc}", "warning")
        return None

    if returncode != 0:
        tail = output_lines[-15:] if output_lines else []
        log(f"AA {attack_label} failed (exit={returncode}):", "warning")
        for line in tail:
            log(f"  {line}", "warning")
        return None

    produced = resolve_produced_aa_output(output_file, attack_label, strength)
    if produced is None or not produced.exists():
        log(f"AA {attack_label} ran but output missing.", "warning")
        return None
    log(f"AA {attack_label} output: {produced.name}", "success")
    return str(produced)
