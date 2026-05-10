"""Stable app-facing similarity adapter over the shared similarity engine."""

from typing import Optional, Callable, Dict, Any

SIMILARITY_PASS_THRESHOLD = 80
_ENGINE = None
_ENGINE_ERROR: Optional[str] = None


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
        return _ENGINE
    except Exception as exc:
        _ENGINE_ERROR = f"similarity backend unavailable: {exc}"
        _log(report_cb, _ENGINE_ERROR, "warning")
        return None


def _diag_summary(diag: Dict[str, Any]) -> str:
    return (
        f"mode={diag.get('mode')} model={diag.get('model_name')} detector={diag.get('detector_backend')} "
        f"faces={diag.get('face_counts')} boxes={diag.get('selected_face_boxes')} conf={diag.get('selected_face_confidence')} "
        f"crop={diag.get('crop_dimensions')} dist={diag.get('raw_cosine_distance')} mapped={diag.get('mapped_score')} "
        f"fallback_reason={diag.get('fallback_reason')}"
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
