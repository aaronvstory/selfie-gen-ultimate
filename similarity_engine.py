"""Shared face similarity backend with diagnostics-first strategy comparison."""

import math
import os
import threading
import tempfile
import urllib.request
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image

# Configure DeepFace backend before import.
try:
    from kling_gui.ml_backend_env import ensure_ml_backend_env
except Exception:
    def ensure_ml_backend_env() -> None:
        if not os.environ.get("TF_USE_LEGACY_KERAS"):
            os.environ["TF_USE_LEGACY_KERAS"] = "1"
        if not os.environ.get("KERAS_BACKEND"):
            os.environ["KERAS_BACKEND"] = "tensorflow"


ensure_ml_backend_env()
from deepface import DeepFace

logger = logging.getLogger(__name__)


class FaceEngine:
    """Singleton backend for detection, extraction, and similarity scoring."""

    _instance = None
    _lock = threading.Lock()
    MODE_EXISTING = "existing"
    MODE_FULL_IMAGE_ALIGN = "full_image_align"
    MODE_NORMALIZED_CROP = "normalized_crop"
    MODE_FALLBACK = "opencv_fallback"

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(FaceEngine, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.model_name = "ArcFace"
        self.detector_backend = "retinaface"
        self.threshold = 0.68
        self.normalized_face_size = (224, 224)
        self.normalized_face_padding = 0.30

        self.models_dir = os.path.join(os.path.dirname(__file__), "similarity_models")
        self.prototxt_path = os.path.join(self.models_dir, "deploy.prototxt")
        self.caffemodel_path = os.path.join(
            self.models_dir, "res10_300x300_ssd_iter_140000.caffemodel"
        )

        self.prototxt_url = (
            "https://raw.githubusercontent.com/opencv/opencv/master/"
            "samples/dnn/face_detector/deploy.prototxt"
        )
        self.caffemodel_url = (
            "https://raw.githubusercontent.com/opencv/opencv_3rdparty/"
            "dnn_samples_face_detector_20170830/"
            "res10_300x300_ssd_iter_140000.caffemodel"
        )

        self.extraction_net = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._initialized = True

    def initialize_models(self) -> None:
        """Warm heavy ArcFace model in memory."""
        try:
            DeepFace.build_model(model_name=self.model_name)
        except Exception as exc:
            raise RuntimeError(f"Failed to initialize Face Models: {exc}") from exc

    def initialize_async(self) -> Future:
        """Warm ArcFace model on a background worker."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="face-engine"
            )
        return self._executor.submit(self.initialize_models)

    def shutdown(self) -> None:
        """Release background executor resources."""
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self._executor.shutdown(wait=False)
            self._executor = None

    def _ensure_extraction_models(self) -> None:
        """Download OpenCV DNN detector files if missing."""
        if not os.path.exists(self.models_dir):
            os.makedirs(self.models_dir)
        if not os.path.exists(self.prototxt_path):
            urllib.request.urlretrieve(self.prototxt_url, self.prototxt_path)
        if not os.path.exists(self.caffemodel_path):
            urllib.request.urlretrieve(self.caffemodel_url, self.caffemodel_path)
        if self.extraction_net is None:
            self.extraction_net = cv2.dnn.readNetFromCaffe(
                self.prototxt_path, self.caffemodel_path
            )

    def extract_face(
        self, input_path: str, output_path: str, padding: float = 0.175
    ) -> float:
        """Extract prominent face using OpenCV DNN detector."""
        self._ensure_extraction_models()

        image = cv2.imread(input_path)
        if image is None:
            raise FileNotFoundError(f"Could not read image: {input_path}")

        h, w = image.shape[:2]
        blob = cv2.dnn.blobFromImage(
            cv2.resize(image, (300, 300)),
            scalefactor=1.0,
            size=(300, 300),
            mean=(104.0, 177.0, 123.0),
        )
        self.extraction_net.setInput(blob)
        detections = self.extraction_net.forward()

        best = None
        best_confidence = 0.0
        for idx in range(detections.shape[2]):
            confidence = detections[0, 0, idx, 2]
            if confidence > 0.5 and confidence > best_confidence:
                best_confidence = confidence
                best = detections[0, 0, idx, 3:7]

        if best is None:
            raise RuntimeError("No face detected in the image.")

        x1, y1, x2, y2 = (best * np.array([w, h, w, h])).astype(int)
        face_w, face_h = x2 - x1, y2 - y1
        pad_x = int(face_w * padding)
        pad_y = int(face_h * padding)

        x1 = max(0, x1 - pad_x)
        y1 = max(0, y1 - pad_y)
        x2 = min(w, x2 + pad_x)
        y2 = min(h, y2 + pad_y)

        face_crop = image[y1:y2, x1:x2]
        cv2.imwrite(output_path, face_crop)
        return float(best_confidence)

    def validate_image_file(self, image_path: str) -> None:
        """Check file exists and is readable by PIL/OpenCV."""
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"File not found: {image_path}")
        try:
            with Image.open(image_path) as img:
                img.verify()
        except Exception as exc:
            raise ValueError(
                f"Corrupted or invalid image file: {image_path} ({exc})"
            ) from exc
        cv_img = cv2.imread(image_path)
        if cv_img is None:
            raise ValueError(f"Unable to read image data via OpenCV: {image_path}")

    def _face_bbox(self, face_data: Dict[str, Any]) -> Dict[str, int]:
        area = face_data.get("facial_area", {}) if isinstance(face_data, dict) else {}
        return {
            "x": int(area.get("x", 0) or 0),
            "y": int(area.get("y", 0) or 0),
            "w": max(0, int(area.get("w", 0) or 0)),
            "h": max(0, int(area.get("h", 0) or 0)),
        }

    def _face_area(self, face_data: Dict[str, Any]) -> int:
        box = self._face_bbox(face_data)
        return int(box["w"] * box["h"])

    def _face_confidence(self, face_data: Dict[str, Any]) -> Optional[float]:
        if not isinstance(face_data, dict):
            return None
        raw = face_data.get("confidence", face_data.get("score"))
        if raw is None:
            return None
        try:
            return float(raw)
        except Exception:
            return None

    def _face_center_distance_sq(self, face_data: Dict[str, Any], width: int, height: int) -> float:
        box = self._face_bbox(face_data)
        cx = float(box["x"]) + (float(box["w"]) / 2.0)
        cy = float(box["y"]) + (float(box["h"]) / 2.0)
        ox = cx - (width / 2.0)
        oy = cy - (height / 2.0)
        return (ox * ox) + (oy * oy)

    def _select_prominent_face(
        self,
        faces: Any,
        image_label: str,
        image_shape: Optional[Tuple[int, int]] = None,
    ) -> Dict[str, Any]:
        if not faces:
            raise ValueError(f"No face detected in {image_label}.")
        width, height = image_shape if image_shape else (1, 1)

        def _sort_key(pair: Tuple[int, Dict[str, Any]]) -> Tuple[float, float, float, int]:
            idx, face = pair
            confidence = self._face_confidence(face)
            conf_value = confidence if confidence is not None else -1.0
            return (
                -float(self._face_area(face)),
                self._face_center_distance_sq(face, width, height),
                -float(conf_value),
                idx,
            )

        return sorted(enumerate(faces), key=_sort_key)[0][1]

    @staticmethod
    def _is_backend_runtime_error(exc: Exception) -> bool:
        lowered = str(exc).lower()
        return (
            "kerastensor" in lowered
            or "tensorflow function" in lowered
            or "symbolic placeholder" in lowered
        )

    def _represent_face(self, face_input: Any) -> np.ndarray:
        representations = DeepFace.represent(
            img_path=face_input,
            model_name=self.model_name,
            detector_backend="skip",
            enforce_detection=False,
            align=False,
        )
        if not representations:
            raise ValueError("Could not generate a face embedding.")
        embedding = representations[0].get("embedding")
        if not embedding:
            raise ValueError("DeepFace did not return an embedding.")
        return np.asarray(embedding, dtype=float)

    def _represent_full_image_with_detection(self, img_path: str) -> List[Dict[str, Any]]:
        reps = DeepFace.represent(
            img_path=img_path,
            model_name=self.model_name,
            detector_backend=self.detector_backend,
            enforce_detection=True,
            align=True,
        )
        return reps or []

    @staticmethod
    def _cosine_distance(embedding1: np.ndarray, embedding2: np.ndarray) -> float:
        norm1 = float(np.linalg.norm(embedding1))
        norm2 = float(np.linalg.norm(embedding2))
        if math.isclose(norm1, 0.0) or math.isclose(norm2, 0.0):
            raise ValueError("Received a zero-length face embedding.")
        similarity = float(np.dot(embedding1, embedding2) / (norm1 * norm2))
        similarity = max(-1.0, min(1.0, similarity))
        return 1.0 - similarity

    def _score_from_distance(self, distance: float) -> Tuple[float, bool]:
        distance = max(0.0, min(1.0, distance))
        if distance <= self.threshold:
            return 100.0 - ((distance / self.threshold) * 20.0), True
        return max(0.0, 79.0 - (((distance - self.threshold) / (1.0 - self.threshold)) * 79.0)), False

    def _extract_faces(self, img_path: str) -> List[Dict[str, Any]]:
        return DeepFace.extract_faces(
            img_path=img_path,
            detector_backend=self.detector_backend,
            enforce_detection=True,
            align=True,
        ) or []

    def _clip_box(self, box: Dict[str, int], width: int, height: int) -> Dict[str, int]:
        x = max(0, min(int(box.get("x", 0)), max(0, width - 1)))
        y = max(0, min(int(box.get("y", 0)), max(0, height - 1)))
        w = max(1, min(int(box.get("w", 1)), width - x))
        h = max(1, min(int(box.get("h", 1)), height - y))
        return {"x": x, "y": y, "w": w, "h": h}

    def _padded_box(self, box: Dict[str, int], width: int, height: int) -> Dict[str, int]:
        pad_x = int(round(box["w"] * self.normalized_face_padding))
        pad_y = int(round(box["h"] * self.normalized_face_padding))
        x1 = max(0, box["x"] - pad_x)
        y1 = max(0, box["y"] - pad_y)
        x2 = min(width, box["x"] + box["w"] + pad_x)
        y2 = min(height, box["y"] + box["h"] + pad_y)
        return {"x": x1, "y": y1, "w": max(1, x2 - x1), "h": max(1, y2 - y1)}

    def _normalized_face_from_image(
        self,
        img_path: str,
        face_data: Dict[str, Any],
        image_label: str,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        image = cv2.imread(img_path)
        if image is None:
            raise FileNotFoundError(f"Could not read image: {img_path}")
        h, w = image.shape[:2]
        base_box = self._clip_box(self._face_bbox(face_data), w, h)
        crop_box = self._padded_box(base_box, w, h)
        crop = image[crop_box["y"] : crop_box["y"] + crop_box["h"], crop_box["x"] : crop_box["x"] + crop_box["w"]]
        if crop.size == 0:
            raise ValueError(f"Normalized crop empty in {image_label}.")
        crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        normalized = cv2.resize(crop_rgb, self.normalized_face_size, interpolation=cv2.INTER_LINEAR)
        return normalized, {
            "bbox": base_box,
            "confidence": self._face_confidence(face_data),
            "crop_dimensions": {
                "normalized_w": int(normalized.shape[1]),
                "normalized_h": int(normalized.shape[0]),
                "source_crop_w": int(crop_box["w"]),
                "source_crop_h": int(crop_box["h"]),
            },
        }

    def _base_mode_diag(self, mode: str, img1_path: str, img2_path: str) -> Dict[str, Any]:
        return {
            "mode": mode,
            "ref_path": img1_path,
            "target_path": img2_path,
            "model_name": self.model_name,
            "detector_backend": self.detector_backend,
            "face_counts": {"ref": 0, "target": 0},
            "selected_face_boxes": {"ref": None, "target": None},
            "selected_face_confidence": {"ref": None, "target": None},
            "crop_dimensions": {"ref": None, "target": None},
            "raw_cosine_distance": None,
            "mapped_score": None,
            "fallback_reason": None,
        }

    def _mode_result(
        self,
        mode: str,
        score: Optional[float],
        match: Optional[bool],
        error: Optional[str],
        diag: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = {
            "mode": mode,
            "score": score,
            "match": match,
            "error": error,
            "diagnostics": diag,
        }
        if score is not None:
            payload["diagnostics"]["mapped_score"] = round(float(score), 2)
        return payload

    def _compare_existing(self, img1_path: str, img2_path: str) -> Dict[str, Any]:
        diag = self._base_mode_diag(self.MODE_EXISTING, img1_path, img2_path)
        faces1 = self._extract_faces(img1_path)
        faces2 = self._extract_faces(img2_path)
        diag["face_counts"] = {"ref": len(faces1), "target": len(faces2)}

        src_img = cv2.imread(img1_path)
        tgt_img = cv2.imread(img2_path)
        if src_img is None or tgt_img is None:
            raise ValueError("Unable to read image data via OpenCV during existing mode.")

        face1 = self._select_prominent_face(faces1, "image 1", (src_img.shape[1], src_img.shape[0]))
        diag["selected_face_boxes"]["ref"] = self._face_bbox(face1)
        diag["selected_face_confidence"]["ref"] = self._face_confidence(face1)

        emb1 = self._represent_face(face1["face"])
        best_distance = None
        best_face = None
        for face2 in faces2:
            emb2 = self._represent_face(face2["face"])
            dist = self._cosine_distance(emb1, emb2)
            if best_distance is None or dist < best_distance:
                best_distance = dist
                best_face = face2
        if best_distance is None or best_face is None:
            raise ValueError("Could not generate face embedding(s) for image 2.")

        diag["selected_face_boxes"]["target"] = self._face_bbox(best_face)
        diag["selected_face_confidence"]["target"] = self._face_confidence(best_face)
        diag["raw_cosine_distance"] = round(float(best_distance), 6)
        score, match = self._score_from_distance(float(best_distance))
        return self._mode_result(self.MODE_EXISTING, score, match, None, diag)

    def _compare_full_image_align(self, img1_path: str, img2_path: str) -> Dict[str, Any]:
        diag = self._base_mode_diag(self.MODE_FULL_IMAGE_ALIGN, img1_path, img2_path)
        reps1 = self._represent_full_image_with_detection(img1_path)
        reps2 = self._represent_full_image_with_detection(img2_path)
        diag["face_counts"] = {"ref": len(reps1), "target": len(reps2)}
        src = self._select_prominent_face(reps1, "image 1")
        tgt = self._select_prominent_face(reps2, "image 2")

        emb1 = np.asarray(src.get("embedding"), dtype=float)
        emb2 = np.asarray(tgt.get("embedding"), dtype=float)
        dist = self._cosine_distance(emb1, emb2)

        diag["selected_face_boxes"]["ref"] = self._face_bbox(src)
        diag["selected_face_boxes"]["target"] = self._face_bbox(tgt)
        diag["selected_face_confidence"]["ref"] = self._face_confidence(src)
        diag["selected_face_confidence"]["target"] = self._face_confidence(tgt)
        diag["raw_cosine_distance"] = round(float(dist), 6)

        score, match = self._score_from_distance(float(dist))
        return self._mode_result(self.MODE_FULL_IMAGE_ALIGN, score, match, None, diag)

    def _compare_normalized_crop(self, img1_path: str, img2_path: str) -> Dict[str, Any]:
        diag = self._base_mode_diag(self.MODE_NORMALIZED_CROP, img1_path, img2_path)
        faces1 = self._extract_faces(img1_path)
        faces2 = self._extract_faces(img2_path)
        diag["face_counts"] = {"ref": len(faces1), "target": len(faces2)}

        src_img = cv2.imread(img1_path)
        tgt_img = cv2.imread(img2_path)
        if src_img is None or tgt_img is None:
            raise ValueError("Unable to read image data via OpenCV during normalized mode.")

        src_face = self._select_prominent_face(faces1, "image 1", (src_img.shape[1], src_img.shape[0]))
        tgt_face = self._select_prominent_face(faces2, "image 2", (tgt_img.shape[1], tgt_img.shape[0]))

        src_norm, src_diag = self._normalized_face_from_image(img1_path, src_face, "image 1")
        tgt_norm, tgt_diag = self._normalized_face_from_image(img2_path, tgt_face, "image 2")

        diag["selected_face_boxes"]["ref"] = src_diag["bbox"]
        diag["selected_face_boxes"]["target"] = tgt_diag["bbox"]
        diag["selected_face_confidence"]["ref"] = src_diag["confidence"]
        diag["selected_face_confidence"]["target"] = tgt_diag["confidence"]
        diag["crop_dimensions"]["ref"] = src_diag["crop_dimensions"]
        diag["crop_dimensions"]["target"] = tgt_diag["crop_dimensions"]

        emb1 = self._represent_face(src_norm)
        emb2 = self._represent_face(tgt_norm)
        dist = self._cosine_distance(emb1, emb2)
        diag["raw_cosine_distance"] = round(float(dist), 6)

        score, match = self._score_from_distance(float(dist))
        return self._mode_result(self.MODE_NORMALIZED_CROP, score, match, None, diag)

    def _compare_with_opencv_fallback(
        self, img1_path: str, img2_path: str, fallback_reason: str
    ) -> Dict[str, Any]:
        diag = self._base_mode_diag(self.MODE_FALLBACK, img1_path, img2_path)
        diag["fallback_reason"] = fallback_reason
        fd1, fallback1 = tempfile.mkstemp(suffix=".png", prefix="_fallback_source_")
        fd2, fallback2 = tempfile.mkstemp(suffix=".png", prefix="_fallback_target_")
        os.close(fd1)
        os.close(fd2)
        try:
            conf1 = self.extract_face(img1_path, fallback1)
            conf2 = self.extract_face(img2_path, fallback2)
            emb1 = self._represent_face(fallback1)
            emb2 = self._represent_face(fallback2)
            dist = self._cosine_distance(emb1, emb2)
            score, match = self._score_from_distance(float(dist))
            diag["face_counts"] = {"ref": 1, "target": 1}
            diag["selected_face_confidence"]["ref"] = conf1
            diag["selected_face_confidence"]["target"] = conf2
            diag["raw_cosine_distance"] = round(float(dist), 6)
            diag["crop_dimensions"]["ref"] = {
                "normalized_w": self.normalized_face_size[0],
                "normalized_h": self.normalized_face_size[1],
            }
            diag["crop_dimensions"]["target"] = {
                "normalized_w": self.normalized_face_size[0],
                "normalized_h": self.normalized_face_size[1],
            }
            return self._mode_result(self.MODE_FALLBACK, score, match, None, diag)
        finally:
            for tmp_path in (fallback1, fallback2):
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    logger.debug("Could not remove temporary fallback file: %s", tmp_path)

    def _build_final_result(
        self,
        chosen: Optional[Dict[str, Any]],
        mode_results: List[Dict[str, Any]],
        err: Optional[str],
    ) -> Dict[str, Any]:
        if chosen is not None and chosen.get("error") is None:
            diagnostics = dict(chosen.get("diagnostics") or {})
            diagnostics["mode_results"] = mode_results
            return {
                "match": bool(chosen.get("match", False)),
                "score": float(chosen.get("score", 0.0)),
                "error": None,
                "diagnostics": diagnostics,
            }

        fallback_diag = self._base_mode_diag("unavailable", "", "")
        fallback_diag["mode_results"] = mode_results
        return {
            "match": False,
            "score": 0.0,
            "error": err or "similarity backend unavailable",
            "diagnostics": fallback_diag,
        }

    def compare_images(self, img1_path: str, img2_path: str) -> Dict[str, Any]:
        """Compare two images with strategy diagnostics and stable fallback ordering."""
        mode_results: List[Dict[str, Any]] = []
        try:
            self.validate_image_file(img1_path)
            self.validate_image_file(img2_path)

            fallback_runtime_reason: Optional[str] = None
            mode_order = [
                self.MODE_NORMALIZED_CROP,
                self.MODE_FULL_IMAGE_ALIGN,
                self.MODE_EXISTING,
            ]
            runners = {
                self.MODE_NORMALIZED_CROP: self._compare_normalized_crop,
                self.MODE_FULL_IMAGE_ALIGN: self._compare_full_image_align,
                self.MODE_EXISTING: self._compare_existing,
            }

            chosen: Optional[Dict[str, Any]] = None
            for mode in mode_order:
                try:
                    result = runners[mode](img1_path, img2_path)
                    mode_results.append(result)
                    if chosen is None and result.get("error") is None:
                        chosen = result
                except Exception as exc:
                    reason = str(exc)
                    if self._is_backend_runtime_error(exc):
                        fallback_runtime_reason = reason
                    mode_results.append(
                        self._mode_result(
                            mode,
                            None,
                            None,
                            reason,
                            self._base_mode_diag(mode, img1_path, img2_path),
                        )
                    )

            if chosen is not None:
                return self._build_final_result(chosen, mode_results, None)

            if fallback_runtime_reason is not None:
                fallback_result = self._compare_with_opencv_fallback(
                    img1_path,
                    img2_path,
                    fallback_runtime_reason,
                )
                mode_results.append(fallback_result)
                return self._build_final_result(fallback_result, mode_results, None)

            error_text = "; ".join(
                [
                    f"{entry.get('mode')}: {entry.get('error')}"
                    for entry in mode_results
                    if entry.get("error")
                ]
            )
            return self._build_final_result(None, mode_results, f"All similarity modes failed: {error_text}")

        except FileNotFoundError as exc:
            return {
                "match": False,
                "score": 0.0,
                "error": str(exc),
                "diagnostics": {
                    "mode": "validation",
                    "ref_path": img1_path,
                    "target_path": img2_path,
                    "mode_results": mode_results,
                },
            }
        except ValueError as exc:
            error_msg = str(exc).lower()
            if "face could not be detected" in error_msg or "no face detected" in error_msg:
                return {
                    "match": False,
                    "score": 0.0,
                    "error": "No face detected in one or both images. Ensure faces are clearly visible.",
                    "diagnostics": {
                        "mode": "validation",
                        "ref_path": img1_path,
                        "target_path": img2_path,
                        "mode_results": mode_results,
                    },
                }
            return {
                "match": False,
                "score": 0.0,
                "error": f"Validation Error: {exc}",
                "diagnostics": {
                    "mode": "validation",
                    "ref_path": img1_path,
                    "target_path": img2_path,
                    "mode_results": mode_results,
                },
            }
        except MemoryError:
            return {
                "match": False,
                "score": 0.0,
                "error": "Memory allocation error. The system ran out of RAM during processing.",
                "diagnostics": {
                    "mode": "runtime",
                    "ref_path": img1_path,
                    "target_path": img2_path,
                    "mode_results": mode_results,
                },
            }
        except Exception as exc:
            error_msg = str(exc).lower()
            if "exhausted" in error_msg or "oom" in error_msg or "memory" in error_msg:
                return {
                    "match": False,
                    "score": 0.0,
                    "error": "Memory resource exhausted. Please free up RAM.",
                    "diagnostics": {
                        "mode": "runtime",
                        "ref_path": img1_path,
                        "target_path": img2_path,
                        "mode_results": mode_results,
                    },
                }
            return {
                "match": False,
                "score": 0.0,
                "error": f"An unexpected ML error occurred: {exc}",
                "diagnostics": {
                    "mode": "runtime",
                    "ref_path": img1_path,
                    "target_path": img2_path,
                    "mode_results": mode_results,
                },
            }
