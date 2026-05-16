"""Stable app-facing similarity adapter over the shared similarity engine."""

import json
import os
from typing import Optional, Callable, Dict, Any

SIMILARITY_PASS_THRESHOLD = 80
# Raw ArcFace cosine-distance threshold (per similarity/CLAUDE.md). Distances
# at or below this map to >= 80% via the polynomial curve in similarity_engine.
# Mirrored as a public constant so log lines and downstream UI can show it
# alongside the mapped score without re-deriving from engine internals.
RAW_DISTANCE_THRESHOLD = 0.68
_ENGINE = None
_ENGINE_ERROR: Optional[str] = None


def _parse_bool(value: Any) -> Optional[bool]:
    """Tolerant boolean parser for config values that may have round-tripped as strings.

    Accepts true Python bools, common string forms ("true"/"false", "1"/"0",
    "yes"/"no", "on"/"off", case-insensitive). Returns None when the value
    can't be confidently coerced — callers should fall back to defaults.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _apply_config_overrides(engine, report_cb: Optional[Callable[[str, str], None]]) -> None:
    """Apply runtime overrides for ensemble / FAS / secondary model from kling_config.json."""
    try:
        from path_utils import get_config_path
        cfg_path = get_config_path("kling_config.json")
        if not os.path.exists(cfg_path):
            return
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (ImportError, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        _log(report_cb, f"config load failed (using defaults): {exc}", "debug")
        return
    if "automation_similarity_use_ensemble" in cfg:
        parsed = _parse_bool(cfg["automation_similarity_use_ensemble"])
        if parsed is not None:
            engine.use_ensemble = parsed
    if "automation_similarity_anti_spoofing" in cfg:
        parsed = _parse_bool(cfg["automation_similarity_anti_spoofing"])
        if parsed is not None:
            engine.anti_spoofing = parsed
    if "automation_similarity_secondary_model" in cfg:
        secondary = cfg["automation_similarity_secondary_model"]
        if isinstance(secondary, str) and secondary.strip():
            engine.secondary_model_name = secondary.strip()


def _log(report_cb: Optional[Callable[[str, str], None]], msg: str, level: str = "debug") -> None:
    if report_cb:
        report_cb(f"Sim: {msg}", level)


def _get_engine(report_cb: Optional[Callable[[str, str], None]] = None):
    global _ENGINE, _ENGINE_ERROR
    if _ENGINE is not None:
        return _ENGINE
    if _ENGINE_ERROR:
        _log(report_cb, _ENGINE_ERROR, "warning")
        return None
    try:
        from similarity_engine import FaceEngine
        _ENGINE = FaceEngine()
        _apply_config_overrides(_ENGINE, report_cb)
        return _ENGINE
    except Exception as exc:
        _ENGINE_ERROR = f"similarity backend unavailable: {exc}"
        _log(report_cb, _ENGINE_ERROR, "warning")
        return None


def _diag_summary(diag: Dict[str, Any]) -> str:
    # Unified FAS verdict (same source as carousel chip + standalone GUI/CLI).
    try:
        from similarity_engine import FaceEngine
        fas_pair = FaceEngine.summarize_fas_pair(diag)
        fas_summary = (
            f" fas_verdict={fas_pair.get('verdict')} "
            f"fas_ref_status={fas_pair.get('ref_status')} "
            f"fas_target_status={fas_pair.get('target_status')}"
        )
    except Exception:
        fas_summary = ""
    return (
        f"mode={diag.get('mode')} model={diag.get('model_name')} detector={diag.get('detector_backend')} "
        f"faces={diag.get('face_counts')} boxes={diag.get('selected_face_boxes')} conf={diag.get('selected_face_confidence')} "
        f"crop={diag.get('crop_dimensions')} dist={diag.get('raw_cosine_distance')} "
        f"per_model={diag.get('per_model_distances')} mapped={diag.get('mapped_score')} "
        f"fallback_reason={diag.get('fallback_reason')}{fas_summary}"
    )


def compute_face_similarity_details(
    source_path: str,
    target_path: str,
    report_cb: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """Return detailed similarity result for gating and diagnostics."""
    engine = _get_engine(report_cb=report_cb)
    if engine is None:
        return {
            "score": 0,
            "pass": False,
            "error": _ENGINE_ERROR or "similarity backend unavailable",
            "match": False,
            "diagnostics": {
                "mode": "unavailable",
                "ref_path": source_path,
                "target_path": target_path,
            },
        }

    _log(report_cb, f"compare ref={source_path!r} target={target_path!r}", "debug")
    result = engine.compare_images(source_path, target_path)

    score_raw = result.get("score", 0.0)
    try:
        score = max(0, min(100, int(round(float(score_raw)))))
    except Exception:
        score = 0

    error = result.get("error")
    passed = bool(score >= SIMILARITY_PASS_THRESHOLD)
    diagnostics = dict(result.get("diagnostics") or {})
    diagnostics.setdefault("ref_path", source_path)
    diagnostics.setdefault("target_path", target_path)
    diagnostics["mapped_score"] = score

    if error:
        _log(report_cb, str(error), "warning")
        _log(report_cb, _diag_summary(diagnostics), "warning")
    else:
        _log(report_cb, f"score={score}% pass={passed} (threshold={SIMILARITY_PASS_THRESHOLD})", "debug")
        _log(report_cb, _diag_summary(diagnostics), "debug")

    return {
        "score": score,
        "pass": passed,
        "error": error,
        "match": bool(result.get("match", False)),
        "diagnostics": diagnostics,
    }


def compute_face_similarity(
    source_path: str,
    target_path: str,
    report_cb: Optional[Callable[[str, str], None]] = None,
) -> Optional[int]:
    """Return 0-100 similarity score (int) or None if comparison fails."""
    details = compute_face_similarity_details(source_path, target_path, report_cb=report_cb)
    if details.get("error"):
        return None
    return details.get("score")
