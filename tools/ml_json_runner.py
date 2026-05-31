"""Headless JSON entrypoints for the bundled-exe ML subprocess bridge.

The bundled Windows .exe excludes the heavy ML stack (torch/TF/mediapipe/
deepface/cv2) from the PyInstaller bundle and installs it into a side venv on
first use. When the frozen GUI needs Face Crop or face-similarity, it runs THIS
script with the side-venv python and parses a single JSON line from stdout.

This module is import-safe (no heavy imports at module load) and only imports
cv2/deepface inside the subcommand handlers — i.e. only when actually run by the
side-venv python, never at GUI import time.

Contract: prints exactly ONE line of JSON to stdout (the result), and nothing
else on stdout (logs/progress go to stderr). Exit 0 on success, non-zero on a
hard failure (the JSON still carries an "error" field when possible).

Usage:
    python -m tools.ml_json_runner crop  --input IMG --output OUT [--multiplier 1.5]
    python -m tools.ml_json_runner similarity --ref IMG1 --target IMG2

Crop result JSON:
    {"ok": true, "output_path": "...", "confidence": 0.42,
     "crop_box": [x0,y0,x1,y1], "extractor": "opencv_multiplier_crop"}
Similarity result JSON (mirrors face_similarity.compute_face_similarity_details):
    {"ok": true, "score": 0-100, "pass": bool, "match": bool,
     "error": null, "diagnostics": {...}}
"""
from __future__ import annotations

import argparse
import json
import sys


def _emit(obj: dict) -> None:
    """Print exactly one JSON line to stdout (the machine-readable result)."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _stderr(msg: str) -> None:
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _cmd_crop(args: argparse.Namespace) -> int:
    """Headless face crop. Reuses face_crop_service.extract_portrait_crop, which
    already returns a dict (output_path/confidence/crop_box/extractor)."""
    try:
        # ML backend env BEFORE any cv2/deepface import (matches the GUI).
        try:
            from kling_gui.ml_backend_env import ensure_ml_backend_env
            ensure_ml_backend_env()
        except Exception:
            import os
            os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
            os.environ.setdefault("KERAS_BACKEND", "tensorflow")

        from face_crop_service import extract_portrait_crop

        result = extract_portrait_crop(
            input_path=args.input,
            output_path=args.output,
            crop_multiplier=args.multiplier,
            progress_cb=lambda m, lvl="info": _stderr(f"[{lvl}] {m}"),
        )
        out = {"ok": True}
        out.update(result)
        _emit(out)
        return 0
    except Exception as exc:  # noqa: BLE001 - report any failure as JSON
        _emit({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return 1


def _cmd_similarity(args: argparse.Namespace) -> int:
    """Headless face similarity. Reuses face_similarity.compute_face_similarity_details
    (the SAME function the in-process GUI uses), so the score/pass/diagnostics
    contract is identical."""
    try:
        try:
            from kling_gui.ml_backend_env import ensure_ml_backend_env
            ensure_ml_backend_env()
        except Exception:
            import os
            os.environ.setdefault("TF_USE_LEGACY_KERAS", "1")
            os.environ.setdefault("KERAS_BACKEND", "tensorflow")

        from face_similarity import compute_face_similarity_details

        details = compute_face_similarity_details(
            args.ref,
            args.target,
            report_cb=lambda m, lvl="info": _stderr(f"[{lvl}] {m}"),
        )
        out = {"ok": not bool(details.get("error"))}
        out.update(details)
        _emit(out)
        return 0
    except Exception as exc:  # noqa: BLE001
        _emit({"ok": False, "score": 0, "pass": False,
               "error": f"{type(exc).__name__}: {exc}"})
        return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ml_json_runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_crop = sub.add_parser("crop", help="Headless face crop -> JSON")
    p_crop.add_argument("--input", required=True)
    p_crop.add_argument("--output", required=True)
    p_crop.add_argument("--multiplier", type=float, default=1.5)
    p_crop.set_defaults(func=_cmd_crop)

    p_sim = sub.add_parser("similarity", help="Headless face similarity -> JSON")
    p_sim.add_argument("--ref", required=True)
    p_sim.add_argument("--target", required=True)
    p_sim.set_defaults(func=_cmd_similarity)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
