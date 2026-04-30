from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set


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


def discover_case_folders(root_dir: Path, front_names: Iterable[str]) -> List[CaseRecord]:
    root = root_dir.resolve()
    canonical_front_names = {name.lower() for name in front_names}
    cases: List[CaseRecord] = []
    pending: List[Path] = [root]

    while pending:
        current = pending.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue

        front_match: Optional[Path] = None
        for child in children:
            if child.is_file() and child.name.lower() in canonical_front_names:
                front_match = child
                break

        if front_match is not None:
            rel = front_match.parent.relative_to(root)
            rel_key = "." if str(rel) == "." else str(rel).replace("\\", "/")
            cases.append(CaseRecord(case_dir=front_match.parent, front_path=front_match, relative_key=rel_key))
            continue

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

    selfie_candidates = []
    video_candidates = []
    for base in (top, gen_images, gen_videos):
        if not base.exists() or not base.is_dir():
            continue
        for item in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file():
                continue
            suffix = item.suffix.lower()
            lname = item.name.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".webp"} and ("selfie" in lname or "sim" in lname):
                selfie_candidates.append(item)
            if suffix == ".mp4":
                video_candidates.append(item)

    return ExistingOutputs(
        front_expanded=front_expanded,
        extracted=extracted,
        selfie_candidate=selfie_candidates[0] if selfie_candidates else None,
        video_candidate=video_candidates[0] if video_candidates else None,
    )


def summarize_cases(root_dir: Path, front_names: Iterable[str]) -> List[Dict[str, str]]:
    records = discover_case_folders(root_dir=root_dir, front_names=front_names)
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

