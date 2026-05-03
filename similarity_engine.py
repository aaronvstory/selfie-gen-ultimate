"""Shared face similarity backend (vendored from C:/claude/similarity/src/engine.py)."""

import math
import os
import threading
import tempfile
import urllib.request
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, Optional, Union

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

    def _face_area(self, face_data: Dict[str, Any]) -> int:
        facial_area = face_data.get("facial_area", {}) if isinstance(face_data, dict) else {}
        width = facial_area.get("w", 0)
        height = facial_area.get("h", 0)
        try:
            return int(max(0, width) * max(0, height))
        except Exception:
            return 0

    def _select_prominent_face(self, faces: Any, image_label: str) -> Any:
        if not faces:
            raise ValueError(f"No face detected in {image_label}.")
        return max(faces, key=self._face_area)

    @staticmethod
    def _is_backend_runtime_error(exc: Exception) -> bool:
        lowered = str(exc).lower()
        return (
            "kerastensor" in lowered
            or "tensorflow function" in lowered
            or "symbolic placeholder" in lowered
        )

    def _represent_faces(self, faces: Any, image_label: str) -> list:
        """Represent all valid extracted faces; raises if none can be embedded."""
        vectors = []
        for idx, face in enumerate(faces or []):
            try:
                vectors.append(self._represent_face(face["face"]))
            except Exception:
                # Ignore individual failures; keep any successful embeddings.
                continue
        if not vectors:
            raise ValueError(f"Could not generate face embedding(s) for {image_label}.")
        return vectors

    def _represent_face(self, face_crop: Any) -> np.ndarray:
        representations = DeepFace.represent(
            img_path=face_crop,
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

    def _compare_with_opencv_fallback(
        self, img1_path: str, img2_path: str
    ) -> Dict[str, Union[bool, float, str]]:
        fd1, fallback1 = tempfile.mkstemp(suffix=".png", prefix="_fallback_source_")
        fd2, fallback2 = tempfile.mkstemp(suffix=".png", prefix="_fallback_target_")
        os.close(fd1)
        os.close(fd2)
        try:
            self.extract_face(img1_path, fallback1)
            self.extract_face(img2_path, fallback2)
            embedding1 = self._represent_face(fallback1)
            embedding2 = self._represent_face(fallback2)
        finally:
            for tmp_path in (fallback1, fallback2):
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    logger.debug("Could not remove temporary fallback file: %s", tmp_path)
        distance = self._cosine_distance(embedding1, embedding2)
        threshold = 0.68

        distance = max(0.0, min(1.0, distance))
        if distance <= threshold:
            similarity_score = 100.0 - ((distance / threshold) * 20.0)
            is_match = True
        else:
            similarity_score = max(
                0.0,
                79.0 - (((distance - threshold) / (1.0 - threshold)) * 79.0),
            )
            is_match = False
        return {"match": is_match, "score": round(similarity_score, 2), "error": None}

    @staticmethod
    def _cosine_distance(embedding1: np.ndarray, embedding2: np.ndarray) -> float:
        norm1 = float(np.linalg.norm(embedding1))
        norm2 = float(np.linalg.norm(embedding2))
        if math.isclose(norm1, 0.0) or math.isclose(norm2, 0.0):
            raise ValueError("Received a zero-length face embedding.")
        similarity = float(np.dot(embedding1, embedding2) / (norm1 * norm2))
        similarity = max(-1.0, min(1.0, similarity))
        return 1.0 - similarity

    def compare_images(self, img1_path: str, img2_path: str) -> Dict[str, Union[bool, float, str]]:
        """Compare two images and return match status and score."""
        try:
            self.validate_image_file(img1_path)
            self.validate_image_file(img2_path)

            try:
                faces1 = DeepFace.extract_faces(
                    img_path=img1_path,
                    detector_backend=self.detector_backend,
                    enforce_detection=True,
                )
                faces2 = DeepFace.extract_faces(
                    img_path=img2_path,
                    detector_backend=self.detector_backend,
                    enforce_detection=True,
                )
            except Exception as exc:
                if not self._is_backend_runtime_error(exc):
                    raise
                logger.warning(
                    "DeepFace extraction backend error; retrying compare with OpenCV fallback: %s",
                    exc,
                )
                return self._compare_with_opencv_fallback(img1_path, img2_path)

            logger.debug(
                "Similarity compare faces: source=%d target=%d", len(faces1 or []), len(faces2 or [])
            )

            face1 = self._select_prominent_face(faces1, "image 1")
            embedding1 = self._represent_face(face1["face"])
            target_embeddings = self._represent_faces(faces2, "image 2")
            distance = min(self._cosine_distance(embedding1, emb) for emb in target_embeddings)
            threshold = 0.68

            distance = max(0.0, min(1.0, distance))
            if distance <= threshold:
                similarity_score = 100.0 - ((distance / threshold) * 20.0)
                is_match = True
            else:
                similarity_score = max(
                    0.0,
                    79.0 - (((distance - threshold) / (1.0 - threshold)) * 79.0),
                )
                is_match = False

            return {"match": is_match, "score": round(similarity_score, 2), "error": None}

        except FileNotFoundError as exc:
            return {"match": False, "score": 0.0, "error": str(exc)}
        except ValueError as exc:
            error_msg = str(exc).lower()
            if "face could not be detected" in error_msg:
                return {
                    "match": False,
                    "score": 0.0,
                    "error": "No face detected in one or both images. Ensure faces are clearly visible.",
                }
            return {"match": False, "score": 0.0, "error": f"Validation Error: {exc}"}
        except MemoryError:
            return {
                "match": False,
                "score": 0.0,
                "error": "Memory allocation error. The system ran out of RAM during processing.",
            }
        except Exception as exc:
            error_msg = str(exc).lower()
            if "exhausted" in error_msg or "oom" in error_msg or "memory" in error_msg:
                return {
                    "match": False,
                    "score": 0.0,
                    "error": "Memory resource exhausted. Please free up RAM.",
                }
            return {
                "match": False,
                "score": 0.0,
                "error": f"An unexpected ML error occurred: {exc}",
            }
