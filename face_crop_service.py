from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

try:
    import cv2
except Exception:  # pragma: no cover - runtime fallback
    cv2 = None

from similarity_engine import FaceEngine


def _report(progress_cb: Optional[Callable[[str, str], None]], message: str, level: str = "info") -> None:
    if progress_cb:
        progress_cb(message, level)


def _detect_face_box_opencv(image_bgr):
    if cv2 is None:
        return None
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    classifier = cv2.CascadeClassifier(cascade_path)
    if classifier.empty():
        return None
    faces = classifier.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    if faces is None or len(faces) == 0:
        return None
    best = max(faces, key=lambda f: int(f[2]) * int(f[3]))
    fx, fy, fw, fh = [int(v) for v in best]
    return fx, fy, fw, fh


def extract_portrait_crop(
    input_path: str,
    output_path: str,
    crop_multiplier: float = 1.5,
    aspect_ratio: Tuple[int, int] = (3, 4),
    progress_cb: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, object]:
    """Headless portrait extraction with GUI-style multiplier crop and fallback."""
    image = None
    if cv2 is not None:
        image = cv2.imread(str(input_path))
    if image is None:
        # If cv2 unavailable or file unreadable, use fallback engine extraction.
        engine = FaceEngine()
        confidence = engine.extract_face(input_path, output_path, padding=0.175)
        return {
            "output_path": output_path,
            "confidence": float(confidence),
            "crop_box": None,
            "extractor": "face_engine_padding_fallback",
        }

    _report(progress_cb, f"Extract portrait from {Path(input_path).name}", "task")
    detected = _detect_face_box_opencv(image)
    if detected is None:
        _report(progress_cb, "OpenCV face detect miss; fallback to similarity engine extraction.", "warning")
        engine = FaceEngine()
        confidence = engine.extract_face(input_path, output_path, padding=0.175)
        return {
            "output_path": output_path,
            "confidence": float(confidence),
            "crop_box": None,
            "extractor": "face_engine_padding_fallback",
        }

    fx, fy, fw, fh = detected
    h_img, w_img = image.shape[:2]
    target_ratio = float(aspect_ratio[1]) / float(aspect_ratio[0])

    face_center_x = fx + (fw // 2)
    face_center_y = fy + (fh // 2)
    target_w = int(fw * crop_multiplier)
    target_h = int(target_w * target_ratio)

    x_start = face_center_x - (target_w // 2)
    y_start = face_center_y - (target_h // 2)
    x_end = x_start + target_w
    y_end = y_start + target_h

    if x_start < 0:
        x_end -= x_start
        x_start = 0
    if x_end > w_img:
        x_start -= x_end - w_img
        x_end = w_img
    if y_start < 0:
        y_end -= y_start
        y_start = 0
    if y_end > h_img:
        y_start -= y_end - h_img
        y_end = h_img

    x_start = max(0, x_start)
    y_start = max(0, y_start)
    x_end = min(w_img, x_end)
    y_end = min(h_img, y_end)

    crop = image[y_start:y_end, x_start:x_end]
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if cv2 is None:
        raise RuntimeError("OpenCV unavailable for crop write")
    write_ok = cv2.imwrite(str(out_path), crop)
    if not write_ok:
        raise RuntimeError(f"Failed to write portrait crop: {output_path}")

    confidence = min(1.0, max(0.0, float(fw * fh) / float(max(1, w_img * h_img)) * 10.0))
    _report(progress_cb, f"Portrait extracted: {out_path.name}", "success")
    return {
        "output_path": str(out_path),
        "confidence": float(confidence),
        "crop_box": [int(x_start), int(y_start), int(x_end), int(y_end)],
        "extractor": "opencv_multiplier_crop",
    }
