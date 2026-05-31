"""Stable app-facing similarity adapter over the shared similarity engine."""

import json
import logging
import os
from typing import Optional, Callable, Dict, Any

_LOGGER = logging.getLogger(__name__)

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
    "yes"/"no", "on"/"off", case-insensitive), AND integer 0/1 (subagent M4
    on PR #53 — a config that programmatically holds the int 1 would
    otherwise return None and surfaces every launch as "marker absent",
    re-firing one-time migrations). Returns None when the value can't be
    confidently coerced — callers should fall back to defaults.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Strict 0/1 only; other ints are ambiguous (e.g. legacy version
        # codes that happened to be stored under a bool-named key).
        if value in (0, 1):
            return bool(value)
        return None
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


def _frozen_similarity_via_subprocess(
    source_path: str,
    target_path: str,
    report_cb: Optional[Callable[[str, str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """In the bundled .exe ONLY, run similarity in the side venv (the bundle
    excludes the ML stack). Returns the result dict, or None to let the caller
    fall through to the normal in-process path (e.g. when not frozen, or the
    bridge is unavailable). Source installs + the automation pipeline never
    enter this path — is_frozen() is False there."""
    if not _IS_FROZEN_BUILD:
        return None
    try:
        import ml_subprocess_bridge as bridge
    except Exception:
        return None

    def _log_msg(msg: str) -> None:
        # Module-level _log(report_cb, msg, level) — adapt to the bridge's
        # single-arg log(msg) callback signature.
        _log(report_cb, msg, "info")

    details = bridge.run_similarity_json(source_path, target_path, log=_log_msg)
    if details is None:
        return {
            "score": 0,
            "pass": False,
            "error": "similarity backend unavailable (ML environment not ready)",
            "match": False,
            "diagnostics": {
                "mode": "unavailable-frozen",
                "ref_path": source_path,
                "target_path": target_path,
            },
        }
    # Normalize the score to the same 0-100 int contract as the in-process path.
    try:
        details["score"] = max(0, min(100, int(round(float(details.get("score", 0))))))
    except Exception:
        details["score"] = 0
    details.setdefault("pass", bool(details["score"] >= SIMILARITY_PASS_THRESHOLD))
    details.setdefault("match", bool(details.get("match", False)))
    details.pop("ok", None)
    return details


def compute_face_similarity_details(
    source_path: str,
    target_path: str,
    report_cb: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, Any]:
    """Return detailed similarity result for gating and diagnostics."""
    # Bundled-exe path: the ML stack lives in a side venv, run as a subprocess.
    # Gated on the module-level _IS_FROZEN_BUILD flag (computed once at import)
    # so source installs + the automation pipeline never even call the helper.
    if _IS_FROZEN_BUILD:
        _frozen = _frozen_similarity_via_subprocess(source_path, target_path, report_cb)
        if _frozen is not None:
            return _frozen
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

    # User feedback 2026-05-22: panel was flooded with `Sim: compare ref=...`
    # + `Sim: score=...` + 200-char `Sim: mode=normalized_crop model=ArcFace
    # detector=... faces=... boxes=... conf=... crop=... dist=... per_model=...
    # mapped=... fallback_reason=... fas_verdict=...` dumps every comparison.
    # These were "debug" level but verbose_gui_mode promotes debug to the
    # panel — and the user wants those lines GONE from the panel. Drop them
    # from the report_cb path entirely so even verbose mode doesn't surface
    # them. The diag dict is still returned in ``result["diagnostics"]`` and
    # the stdlib logger picks it up via ``_LOGGER.debug`` below — so file-log
    # forensic recovery still works.
    _LOGGER.debug("compare ref=%r target=%r", source_path, target_path)
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
        _LOGGER.debug("diag (err): %s", _diag_summary(diagnostics))
    else:
        _LOGGER.debug(
            "score=%d%% pass=%s (threshold=%d) — %s",
            score, passed, SIMILARITY_PASS_THRESHOLD, _diag_summary(diagnostics),
        )

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
