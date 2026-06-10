from __future__ import annotations

from dataclasses import dataclass
import fnmatch
from pathlib import Path
import re
from typing import Callable, Dict, Iterable, List, Optional, Set


IGNORED_DIR_NAMES: Set[str] = {
    "gen-images",
    "gen-videos",
    "sessions",
    ".git",
    "venv",
    "__pycache__",
    "oldcam-v7",
    "oldcam-v8",
}


def is_ignored_dir(path: Path) -> bool:
    name = path.name.lower()
    return name in IGNORED_DIR_NAMES or name.startswith(".venv")


# Image suffixes a --front-glob match is allowed to resolve to. Keeps a loose
# pattern ('*front*') from picking a sidecar (.txt/.json) or a video (.mp4)
# instead of the actual front image. Mirrors path_utils.VALID_EXTENSIONS but is
# kept local so discovery has no cross-module import.
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif")


@dataclass(frozen=True)
class CaseRecord:
    case_dir: Path
    front_path: Path
    relative_key: str


@dataclass(frozen=True)
class ExistingOutputs:
    front_expanded: Optional[Path]
    extracted: Optional[Path]
    selfie_candidate: Optional[Path]
    video_candidate: Optional[Path]


def _find_first_existing(paths: Iterable[Path]) -> Optional[Path]:
    for candidate in paths:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def discover_case_folders(
    root_dir: Path,
    front_names: Iterable[str],
    front_globs: Optional[Iterable[str]] = None,
    warn_cb: Optional[Callable[[str], None]] = None,
) -> List[CaseRecord]:
    """Recursively find case folders that contain a front image.

    A folder qualifies when it directly contains a file whose lowercased name
    EITHER matches one of ``front_names`` exactly (e.g. ``front.jpg``) OR matches
    one of the optional ``front_globs`` patterns (e.g. ``*id_photo*.jpg``).

    ``front_globs`` exists because real-world batch input is not always literally
    named ``front.jpg`` — production folders often carry their own naming
    (``...id_photo-....jpg``). Globs are matched with :func:`fnmatch.fnmatch` on
    the lowercased filename (stdlib, no new dependency). When BOTH lists are
    empty the function returns no cases.

    Within a single folder the children are scanned in sorted order, so the
    first matching file wins deterministically. If more than one file in the same
    folder matches (only possible via a loose glob), ``warn_cb`` — when provided —
    is invoked once for that folder so the operator can tighten the pattern.
    """
    root = root_dir.resolve()
    canonical_front_names = {name.lower() for name in front_names}
    canonical_front_globs = [str(pat).lower() for pat in (front_globs or []) if str(pat).strip()]
    cases: List[CaseRecord] = []
    pending: List[Path] = [root]

    def _matches(name_lower: str) -> bool:
        if name_lower in canonical_front_names:
            return True
        # Glob matches are restricted to image files. A loose pattern like
        # '*front*' must NOT pick up front.txt / front.mp4 / a sidecar before the
        # actual image (Codex review). Exact front_names above are trusted as-is.
        if canonical_front_globs and not any(
            name_lower.endswith(ext) for ext in _IMAGE_SUFFIXES
        ):
            return False
        # fnmatchcase (not fnmatch): name + patterns are already lowercased, so
        # we don't want fnmatch's OS-specific os.path.normcase, which on Windows
        # also rewrites slashes and would make matching non-deterministic across
        # platforms (Gemini review, PR #94).
        return any(fnmatch.fnmatchcase(name_lower, pat) for pat in canonical_front_globs)

    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue

        front_match: Optional[Path] = None
        match_count = 0
        for child in children:
            if child.is_file() and _matches(child.name.lower()):
                match_count += 1
                if front_match is None:
                    front_match = child

        if front_match is not None:
            if match_count > 1 and warn_cb is not None:
                warn_cb(
                    f"{match_count} files match the front pattern in "
                    f"{front_match.parent}; using '{front_match.name}' (first sorted). "
                    "Tighten --front-glob if this is the wrong file."
                )
            rel = front_match.parent.relative_to(root)
            rel_key = "." if str(rel) == "." else str(rel).replace("\\", "/")
            cases.append(CaseRecord(case_dir=front_match.parent, front_path=front_match, relative_key=rel_key))

        for child in reversed(children):
            if child.is_dir() and not is_ignored_dir(child):
                pending.append(child)

    cases.sort(key=lambda case: case.relative_key.lower())
    return cases


def detect_existing_outputs(case_dir: Path) -> ExistingOutputs:
    top = case_dir
    gen_images = case_dir / "gen-images"
    gen_videos = case_dir / "gen-videos"

    front_expanded = _find_first_existing([top / "front-expanded.png", gen_images / "front-expanded.png"])
    extracted = _find_first_existing([top / "extracted.png", gen_images / "extracted.png"])

    selfie_candidates: List[Path] = []
    video_candidates: List[Path] = []
    # Matches the similarity token in generated-selfie names: bare "sim"
    # AND the real-world scored form "sim88" (E2E round 0, 2026-06-11: the
    # bare-token-only pattern never matched `..._sim88_001.png`, so existing
    # selfies were silently REGENERATED — a paid API call — on every rerun).
    sim_token_re = re.compile(r"(^|[_\-. ])sim\d*($|[_\-. ])")
    for base in (gen_images, top):
        if not base.exists() or not base.is_dir():
            continue
        for item in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file():
                continue
            suffix = item.suffix.lower()
            lname = item.name.lower()
            sim_token = bool(sim_token_re.search(lname))
            if suffix in {".png", ".jpg", ".jpeg", ".webp"} and ("selfie" in lname or sim_token):
                selfie_candidates.append(item)
    # Prefer generated videos under gen-videos, and avoid oldcam artifacts.
    oldcam_token_re = re.compile(r"(^|[_\-. ])oldcam([_\-. ]|$)|([_\-. ])v(7|8)([_\-. ]|$)")
    generated_video_hint_re = re.compile(r"(^|[_\-. ])(kling|video|generated)([_\-. ]|$)")
    for base in (gen_videos, top):
        if not base.exists() or not base.is_dir():
            continue
        for item in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file() or item.suffix.lower() != ".mp4":
                continue
            lname = item.name.lower()
            if oldcam_token_re.search(lname):
                continue
            if base == top and not generated_video_hint_re.search(lname):
                continue
            video_candidates.append(item)

    def _best_selfie_candidate(candidates: List[Path]) -> Optional[Path]:
        if not candidates:
            return None
        now = max((c.stat().st_mtime for c in candidates if c.exists()), default=0.0)
        ranked = sorted(
            candidates,
            key=lambda p: (
                1 if p.parent == gen_images else 0,
                1 if "selfie" in p.name.lower() else 0,
                p.stat().st_mtime if p.exists() else now,
                p.name.lower(),
            ),
            reverse=True,
        )
        return ranked[0] if ranked else None

    best_selfie = _best_selfie_candidate(selfie_candidates)
    video_candidates = sorted(
        video_candidates,
        key=lambda p: (
            1 if p.parent == gen_videos else 0,
            p.stat().st_mtime if p.exists() else 0.0,
            p.name.lower(),
        ),
        reverse=True,
    )

    return ExistingOutputs(
        front_expanded=front_expanded,
        extracted=extracted,
        selfie_candidate=best_selfie,
        video_candidate=video_candidates[0] if video_candidates else None,
    )


def summarize_cases(
    root_dir: Path,
    front_names: Iterable[str],
    front_globs: Optional[Iterable[str]] = None,
) -> List[Dict[str, str]]:
    records = discover_case_folders(
        root_dir=root_dir, front_names=front_names, front_globs=front_globs
    )
    summary: List[Dict[str, str]] = []
    for record in records:
        outputs = detect_existing_outputs(record.case_dir)
        summary.append(
            {
                "case_dir": str(record.case_dir),
                "relative_key": record.relative_key,
                "front_path": str(record.front_path),
                "front_expanded": str(outputs.front_expanded) if outputs.front_expanded else "",
                "extracted": str(outputs.extracted) if outputs.extracted else "",
                "selfie": str(outputs.selfie_candidate) if outputs.selfie_candidate else "",
                "video": str(outputs.video_candidate) if outputs.video_candidate else "",
            }
        )
    return summary
