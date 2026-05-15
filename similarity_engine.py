"""Shared face similarity backend with diagnostics-first strategy comparison."""

import math
import os
import threading
import tempfile
import urllib.request
import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

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
    model_name = "ArcFace"
    detector_backend = "retinaface"
    threshold = 0.68
    secondary_model_name = "Facenet512"
    use_ensemble = True
    anti_spoofing = True
    normalized_face_size = (224, 224)
    normalized_face_padding = 0.30
    models_dir = ""
    prototxt_path = ""
    caffemodel_path = ""
    prototxt_url = ""
    caffemodel_url = ""
    extraction_net = None
    _executor: Optional[ThreadPoolExecutor] = None
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
        self.secondary_model_name = "Facenet512"
        self.use_ensemble = True
        self.anti_spoofing = True
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
        """Warm primary + (when ensemble enabled) secondary embedding models in memory."""
        try:
            DeepFace.build_model(model_name=self.model_name)
            if self.use_ensemble:
                try:
                    DeepFace.build_model(model_name=self.secondary_model_name)
                except Exception as exc:
                    logger.warning(
                        "Secondary model warmup failed (%s); ensemble will fall back at runtime: %s",
                        self.secondary_model_name,
                        exc,
                    )
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

    def _represent_face_with_model(self, face_input: Any, model_name: str) -> np.ndarray:
        representations = DeepFace.represent(
            img_path=face_input,
            model_name=model_name,
            detector_backend="skip",
            enforce_detection=False,
            align=False,
        )
        if not representations:
            raise ValueError(f"Could not generate embedding via {model_name}.")
        first = representations[0]
        if isinstance(first, list):
            if not first:
                raise ValueError(f"{model_name} returned empty representation list.")
            first = first[0]
        embedding = first.get("embedding") if isinstance(first, dict) else None
        if not embedding:
            raise ValueError(f"{model_name} did not return an embedding.")
        return np.asarray(embedding, dtype=float)

    def _represent_face(self, face_input: Any) -> np.ndarray:
        return self._represent_face_with_model(face_input, self.model_name)

    def _represent_full_image_with_detection_with_model(
        self, img_path: str, model_name: str
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = dict(
            img_path=img_path,
            model_name=model_name,
            detector_backend=self.detector_backend,
            enforce_detection=True,
            align=True,
        )
        if self.anti_spoofing:
            kwargs["anti_spoofing"] = True
        reps = DeepFace.represent(**kwargs) or []
        # Normalize: some DeepFace builds return List[List[Dict]] when multiple embeddings/models.
        if reps and isinstance(reps[0], list):
            flat: List[Dict[str, Any]] = []
            for inner in reps:
                if isinstance(inner, list):
                    flat.extend([item for item in inner if isinstance(item, dict)])
            return flat
        return [item for item in reps if isinstance(item, dict)]

    def _represent_full_image_with_detection(self, img_path: str) -> List[Dict[str, Any]]:
        return self._represent_full_image_with_detection_with_model(img_path, self.model_name)

    @staticmethod
    def _cosine_distance(embedding1: np.ndarray, embedding2: np.ndarray) -> float:
        norm1 = float(np.linalg.norm(embedding1))
        norm2 = float(np.linalg.norm(embedding2))
        if math.isclose(norm1, 0.0) or math.isclose(norm2, 0.0):
            raise ValueError("Received a zero-length face embedding.")
        similarity = float(np.dot(embedding1, embedding2) / (norm1 * norm2))
        similarity = max(-1.0, min(1.0, similarity))
        return 1.0 - similarity

    def _embed_reference_for_ensemble(
        self, face_input: Any
    ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """Compute primary (and optionally secondary) embeddings for a reference face.

        Returns (primary_emb, secondary_emb_or_None). Used by callers that compare a
        single reference against many targets — embedding the reference once and
        reusing it avoids 2N redundant DeepFace inference calls when ensemble is on.
        """
        primary_emb = self._represent_face_with_model(face_input, self.model_name)
        if not self.use_ensemble:
            return primary_emb, None
        try:
            secondary_emb = self._represent_face_with_model(face_input, self.secondary_model_name)
            return primary_emb, secondary_emb
        except Exception as exc:
            logger.warning(
                "Secondary model %s failed for reference: %s", self.secondary_model_name, exc
            )
            return primary_emb, None

    def _distance_against_cached_ref(
        self,
        target_face: Any,
        ref_primary: np.ndarray,
        ref_secondary: Optional[np.ndarray],
    ) -> Tuple[float, Dict[str, float]]:
        """Distance between a target face and a pre-computed reference embedding pair.

        Mirrors `_ensemble_distance_pair` semantics (avg of two model distances when
        ensemble is on, primary-only fallback if secondary errors), but skips the
        cost of re-embedding the reference. Use when comparing one ref against
        many candidates.
        """
        per_model: Dict[str, float] = {}
        target_primary = self._represent_face_with_model(target_face, self.model_name)
        primary_dist = self._cosine_distance(ref_primary, target_primary)
        per_model[self.model_name] = float(primary_dist)

        if ref_secondary is None:
            return primary_dist, per_model

        try:
            target_secondary = self._represent_face_with_model(target_face, self.secondary_model_name)
            sec_dist = self._cosine_distance(ref_secondary, target_secondary)
            per_model[self.secondary_model_name] = float(sec_dist)
            return (primary_dist + sec_dist) / 2.0, per_model
        except Exception as exc:
            logger.warning(
                "Secondary model %s failed for target: %s", self.secondary_model_name, exc
            )
            return primary_dist, per_model

    def _ensemble_distance_pair(
        self, face_input1: Any, face_input2: Any
    ) -> Tuple[float, Dict[str, float]]:
        """Average cosine distance across primary + secondary models.

        Returns (avg_distance, {model_name: distance}).
        Falls back to primary-only if secondary model errors out.

        For one-ref-against-many-targets workflows, prefer
        `_embed_reference_for_ensemble` + `_distance_against_cached_ref` to avoid
        re-embedding the reference on every call.
        """
        ref_primary, ref_secondary = self._embed_reference_for_ensemble(face_input1)
        return self._distance_against_cached_ref(face_input2, ref_primary, ref_secondary)

    def _check_anti_spoofing(
        self, faces: List[Dict[str, Any]], image_label: str
    ) -> Dict[str, Any]:
        """Inspect FAS verdicts on extracted faces. LOG-ONLY — never raises.

        ALWAYS returns a dict so the UI can render a consistent message per side.
        The `status` field tells callers WHY FAS data is or isn't available:

          - status="ok":          spoof_detected + faces populated normally
          - status="no_face":     no face was detected on this image
          - status="not_active":  FAS was disabled at extraction time (no is_real key)
          - status="error":       unexpected shape from DeepFace

        This replaces the prior None return that made it impossible for the UI
        to distinguish "we don't know" from "this side wasn't checked", causing
        asymmetric ref=None / target={...} renderings.
        """
        if not faces:
            return {"status": "no_face", "spoof_detected": None, "faces": []}
        has_fas_data = any(
            isinstance(f, dict) and "is_real" in f for f in faces
        )
        if not has_fas_data:
            return {"status": "not_active", "spoof_detected": None, "faces": []}
        records: List[Dict[str, Any]] = []
        spoof_detected = False
        for face in faces:
            if not isinstance(face, dict):
                continue
            is_real = face.get("is_real")
            score = face.get("antispoof_score")
            records.append({"is_real": is_real, "antispoof_score": score})
            if is_real is False:
                spoof_detected = True
        if spoof_detected:
            logger.warning(
                "Anti-spoofing flagged possible spoof in %s: %s", image_label, records
            )
        return {"status": "ok", "spoof_detected": spoof_detected, "faces": records}

    @staticmethod
    def _side_real_confidence(side: Optional[Dict[str, Any]]) -> Optional[float]:
        """Reduce a side's per-face FAS records to a single real-confidence number on [0,1].

        DeepFace returns `{is_real: bool, antispoof_score: float}` per face,
        where the score's MEANING flips with the boolean:
          - is_real=True  -> score = confidence the face is REAL
          - is_real=False -> score = confidence the face is a SPOOF

        Renderers historically forwarded `antispoof_score` raw and rendered it
        as "% real" unconditionally — which displayed a definitively-spoofed DL
        (is_real=False, score=0.9999) as "99.99% real". This helper folds the
        boolean into a single derived dimension `real_conf = score if is_real
        else (1 - score)` so downstream code consumes one unambiguous number.

        Returns the MIN real_conf across detected faces (the
        least-confidently-real face is the most informative for "is this image
        trustworthy?"). Returns None if no records are available.

        Faces without an `is_real` key are skipped (they pre-date FAS being
        active and would taint the min).
        """
        if not isinstance(side, dict):
            return None
        faces = side.get("faces") or []
        confs: List[float] = []
        for face in faces:
            if not isinstance(face, dict):
                continue
            score = face.get("antispoof_score")
            is_real = face.get("is_real")
            if not isinstance(score, (int, float)) or not isinstance(is_real, bool):
                continue
            real_conf = float(score) if is_real else (1.0 - float(score))
            real_conf = max(0.0, min(1.0, real_conf))
            confs.append(real_conf)
        if not confs:
            return None
        return min(confs)

    @staticmethod
    def _side_is_real(side: Optional[Dict[str, Any]]) -> Optional[bool]:
        """Return the side's overall is_real verdict.

        If ANY face on the side was flagged is_real=False, the side is False
        (spoof present). Otherwise True if at least one face was checked.
        Returns None when no FAS records are available.
        """
        if not isinstance(side, dict):
            return None
        faces = side.get("faces") or []
        any_checked = False
        for face in faces:
            if not isinstance(face, dict):
                continue
            is_real = face.get("is_real")
            if not isinstance(is_real, bool):
                continue
            any_checked = True
            if is_real is False:
                return False
        return True if any_checked else None

    # Backward-compat alias — some external callers may still use the old name.
    # Returns the raw antispoof_score (un-inverted), which is what the old
    # behavior produced. New code should use _side_real_confidence instead.
    @staticmethod
    def _side_antispoof_score(side: Optional[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(side, dict):
            return None
        faces = side.get("faces") or []
        scores = []
        for face in faces:
            if not isinstance(face, dict):
                continue
            v = face.get("antispoof_score")
            if isinstance(v, (int, float)):
                scores.append(float(v))
        if not scores:
            return None
        return min(scores)

    @staticmethod
    def summarize_fas_pair(diag: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Reduce per-side FAS records into a single, consistent UI verdict.

        Returns a dict with these fields:
          - verdict: "pass" | "fail" | "unavailable"
          - color_hint: "green" | "amber" | "muted"
          - message: user-facing text (verdict line only)
          - ref_status / target_status: per-side status from _check_anti_spoofing
          - ref_real_conf / target_real_conf: float in [0,1], 1.0 = certainly
            real, 0.0 = certainly spoof. ALREADY interpreted — renderers can
            just multiply by 100 to get a "% real" number directly.
          - ref_is_real / target_is_real: bool|None engine verdict per side
            (None if unknown). Renderers should switch on this boolean to pick
            "PASS (Real)" vs "FAIL (Spoof)" labels, NOT on score magnitude.
          - ref_score / target_score: raw antispoof_score (back-compat). NOT
            for display — its meaning depends on is_real. Use real_conf.

        Renderers (carousel chip, standalone GUI label, CLI Rich panel) should
        ALL go through this helper so the same input always produces the same
        output — no more "ref+target sometimes, target only sometimes" drift.
        """
        muted = {
            "verdict": "unavailable",
            "color_hint": "muted",
            "message": "Liveness (anti-spoof): not assessed",
            "ref_status": "missing",
            "target_status": "missing",
            "ref_score": None,
            "target_score": None,
            "ref_real_conf": None,
            "target_real_conf": None,
            "ref_is_real": None,
            "target_is_real": None,
        }
        if not isinstance(diag, dict):
            return muted
        fas = diag.get("anti_spoofing")
        if not isinstance(fas, dict):
            return muted
        ref = fas.get("ref") if isinstance(fas.get("ref"), dict) else None
        tgt = fas.get("target") if isinstance(fas.get("target"), dict) else None
        ref_status = (ref or {}).get("status", "missing") if ref else "missing"
        tgt_status = (tgt or {}).get("status", "missing") if tgt else "missing"
        ref_ok = ref_status == "ok"
        tgt_ok = tgt_status == "ok"
        ref_score = FaceEngine._side_antispoof_score(ref)
        tgt_score = FaceEngine._side_antispoof_score(tgt)
        ref_real_conf = FaceEngine._side_real_confidence(ref)
        tgt_real_conf = FaceEngine._side_real_confidence(tgt)
        ref_is_real = FaceEngine._side_is_real(ref)
        tgt_is_real = FaceEngine._side_is_real(tgt)
        # If EITHER side lacks ok-status FAS data, treat the whole verdict as
        # unavailable — never report a partial ref-only or target-only reading,
        # which is what was confusing the user.
        if not (ref_ok and tgt_ok):
            reasons = []
            if not ref_ok:
                reasons.append(f"ref={ref_status}")
            if not tgt_ok:
                reasons.append(f"target={tgt_status}")
            return {
                "verdict": "unavailable",
                "color_hint": "muted",
                "message": f"Liveness (anti-spoof): not assessed ({', '.join(reasons)})",
                "ref_status": ref_status,
                "target_status": tgt_status,
                "ref_score": ref_score,
                "target_score": tgt_score,
                "ref_real_conf": ref_real_conf,
                "target_real_conf": tgt_real_conf,
                "ref_is_real": ref_is_real,
                "target_is_real": tgt_is_real,
            }
        # Both sides have ok FAS data — render the verdict.
        ref_spoof = bool((ref or {}).get("spoof_detected"))
        tgt_spoof = bool((tgt or {}).get("spoof_detected"))
        if ref_spoof or tgt_spoof:
            flagged = []
            if ref_spoof:
                flagged.append("ref")
            if tgt_spoof:
                flagged.append("target")
            return {
                "verdict": "fail",
                "color_hint": "amber",
                "message": f"Liveness (anti-spoof): possible synthetic input on {' & '.join(flagged)} (advisory only)",
                "ref_status": "ok",
                "target_status": "ok",
                "ref_score": ref_score,
                "target_score": tgt_score,
                "ref_real_conf": ref_real_conf,
                "target_real_conf": tgt_real_conf,
                "ref_is_real": ref_is_real,
                "target_is_real": tgt_is_real,
            }
        return {
            "verdict": "pass",
            "color_hint": "green",
            "message": "Liveness (anti-spoof): both images look real",
            "ref_status": "ok",
            "target_status": "ok",
            "ref_score": ref_score,
            "target_score": tgt_score,
            "ref_real_conf": ref_real_conf,
            "target_real_conf": tgt_real_conf,
            "ref_is_real": ref_is_real,
            "target_is_real": tgt_is_real,
        }

    # Polynomial-easing exponent for the pass curve (distance <= threshold).
    # v1.8 used 2.5, which compressed the entire 0.0-0.20 distance band into
    # 99-100% — making AI-generated selfies (typical distance 0.05-0.15) read
    # as visually indistinguishable from pixel-identical inputs. v1.9 uses 0.5
    # (square root) to spread that band across 95-91% so the score conveys
    # meaningful gradation. Reference points (threshold=0.68):
    #   distance 0.00 -> 100.00%  (identical)
    #   distance 0.05 ->  94.58%  (typical AI selfie, identity preserved)
    #   distance 0.10 ->  92.33%
    #   distance 0.15 ->  90.61%
    #   distance 0.20 ->  89.15%  (visible variance)
    #   distance 0.30 ->  86.72%
    #   distance 0.50 ->  82.85%
    #   distance 0.68 ->  80.00%  (ArcFace official threshold)
    PASS_CURVE_EXPONENT: float = 0.5

    def _score_from_distance(self, distance: float) -> Tuple[float, bool]:
        distance = max(0.0, min(1.0, distance))
        epsilon = 1e-6
        safe_threshold = max(epsilon, min(1.0 - epsilon, float(self.threshold)))
        if distance <= safe_threshold:
            ratio = distance / safe_threshold
            curved_score = 80.0 + (20.0 * (1.0 - math.pow(ratio, self.PASS_CURVE_EXPONENT)))
            return curved_score, True
        fail_ratio = (distance - safe_threshold) / (1.0 - safe_threshold)
        fail_score = max(0.0, 79.0 * (1.0 - math.pow(fail_ratio, 0.5)))
        return fail_score, False

    def _extract_faces(self, img_path: str) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = dict(
            img_path=img_path,
            detector_backend=self.detector_backend,
            enforce_detection=True,
            align=True,
        )
        if self.anti_spoofing:
            kwargs["anti_spoofing"] = True
        faces = DeepFace.extract_faces(**kwargs) or []
        # Normalize: some DeepFace builds may wrap in nested lists.
        if faces and isinstance(faces[0], list):
            flat: List[Dict[str, Any]] = []
            for inner in faces:
                if isinstance(inner, list):
                    flat.extend([item for item in inner if isinstance(item, dict)])
            return flat
        return [item for item in faces if isinstance(item, dict)]

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
            "per_model_distances": None,
            "anti_spoofing": None,
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

        # Embed the reference face ONCE (primary + optional secondary). With ensemble on
        # this is the single biggest perf win: previously we re-embedded the ref against
        # both ArcFace AND Facenet512 for every target face, scaling 2N inference calls
        # per multi-face image. This collapses it to 2 + N (ensemble) or 1 + N (primary-only).
        ref_primary, ref_secondary = self._embed_reference_for_ensemble(face1["face"])
        best_distance: Optional[float] = None
        best_face: Optional[Dict[str, Any]] = None
        best_per_model: Dict[str, float] = {}
        for face2 in faces2:
            try:
                dist, per_model = self._distance_against_cached_ref(
                    face2["face"], ref_primary, ref_secondary
                )
                if best_distance is None or dist < best_distance:
                    best_distance = dist
                    best_face = face2
                    best_per_model = per_model
            except Exception:
                continue
        if best_distance is None or best_face is None:
            raise ValueError("Could not generate face embedding(s) for image 2.")

        diag["selected_face_boxes"]["target"] = self._face_bbox(best_face)
        diag["selected_face_confidence"]["target"] = self._face_confidence(best_face)
        diag["raw_cosine_distance"] = round(float(best_distance), 6)
        diag["per_model_distances"] = {k: round(float(v), 6) for k, v in best_per_model.items()}
        diag["anti_spoofing"] = {
            "ref": self._check_anti_spoofing(faces1, "image 1"),
            "target": self._check_anti_spoofing(faces2, "image 2"),
        }
        score, match = self._score_from_distance(float(best_distance))
        return self._mode_result(self.MODE_EXISTING, score, match, None, diag)

    def _compare_full_image_align(self, img1_path: str, img2_path: str) -> Dict[str, Any]:
        diag = self._base_mode_diag(self.MODE_FULL_IMAGE_ALIGN, img1_path, img2_path)
        reps1 = self._represent_full_image_with_detection_with_model(img1_path, self.model_name)
        reps2 = self._represent_full_image_with_detection_with_model(img2_path, self.model_name)
        diag["face_counts"] = {"ref": len(reps1), "target": len(reps2)}
        src_img = cv2.imread(img1_path)
        tgt_img = cv2.imread(img2_path)
        if src_img is None or tgt_img is None:
            raise ValueError("Unable to read image data via OpenCV during full-image mode.")
        src = self._select_prominent_face(reps1, "image 1", (src_img.shape[1], src_img.shape[0]))
        tgt = self._select_prominent_face(reps2, "image 2", (tgt_img.shape[1], tgt_img.shape[0]))

        emb1 = np.asarray(src.get("embedding"), dtype=float)
        emb2 = np.asarray(tgt.get("embedding"), dtype=float)
        primary_dist = self._cosine_distance(emb1, emb2)
        per_model: Dict[str, float] = {self.model_name: float(primary_dist)}

        dist = primary_dist
        if self.use_ensemble:
            try:
                reps1_sec = self._represent_full_image_with_detection_with_model(
                    img1_path, self.secondary_model_name
                )
                reps2_sec = self._represent_full_image_with_detection_with_model(
                    img2_path, self.secondary_model_name
                )
                src_sec = self._select_prominent_face(
                    reps1_sec, "image 1 (sec)", (src_img.shape[1], src_img.shape[0])
                )
                tgt_sec = self._select_prominent_face(
                    reps2_sec, "image 2 (sec)", (tgt_img.shape[1], tgt_img.shape[0])
                )
                emb1_sec = np.asarray(src_sec.get("embedding"), dtype=float)
                emb2_sec = np.asarray(tgt_sec.get("embedding"), dtype=float)
                sec_dist = self._cosine_distance(emb1_sec, emb2_sec)
                per_model[self.secondary_model_name] = float(sec_dist)
                dist = (primary_dist + sec_dist) / 2.0
            except Exception as exc:
                logger.warning(
                    "Secondary model %s failed in full-image mode: %s",
                    self.secondary_model_name,
                    exc,
                )

        diag["selected_face_boxes"]["ref"] = self._face_bbox(src)
        diag["selected_face_boxes"]["target"] = self._face_bbox(tgt)
        diag["selected_face_confidence"]["ref"] = self._face_confidence(src)
        diag["selected_face_confidence"]["target"] = self._face_confidence(tgt)
        diag["raw_cosine_distance"] = round(float(dist), 6)
        diag["per_model_distances"] = {k: round(float(v), 6) for k, v in per_model.items()}
        diag["anti_spoofing"] = {
            "ref": self._check_anti_spoofing(reps1, "image 1"),
            "target": self._check_anti_spoofing(reps2, "image 2"),
        }

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

        dist, per_model = self._ensemble_distance_pair(src_norm, tgt_norm)
        diag["raw_cosine_distance"] = round(float(dist), 6)
        diag["per_model_distances"] = {k: round(float(v), 6) for k, v in per_model.items()}
        diag["anti_spoofing"] = {
            "ref": self._check_anti_spoofing(faces1, "image 1"),
            "target": self._check_anti_spoofing(faces2, "image 2"),
        }

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
            diag["per_model_distances"] = {self.model_name: round(float(dist), 6)}
            diag["anti_spoofing"] = {"ref": None, "target": None}
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
        img1_path: str,
        img2_path: str,
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

        fallback_diag = self._base_mode_diag("unavailable", img1_path, img2_path)
        fallback_diag["mode_results"] = mode_results
        return {
            "match": False,
            "score": 0.0,
            "error": err or "similarity backend unavailable",
            "diagnostics": fallback_diag,
        }

    def compare_images(
        self, img1_path: str, img2_path: str, diagnostic_matrix: bool = False
    ) -> Dict[str, Any]:
        """Compare two images; run full strategy matrix only when explicitly requested."""
        mode_results: List[Dict[str, Any]] = []
        try:
            self.validate_image_file(img1_path)
            self.validate_image_file(img2_path)

            fallback_runtime_reason: Optional[str] = None
            # Diagnostic-first design: evaluate a small bounded strategy set for stability analysis.
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
                        if not diagnostic_matrix:
                            break
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
                return self._build_final_result(chosen, mode_results, None, img1_path, img2_path)

            if fallback_runtime_reason is not None:
                fallback_result = self._compare_with_opencv_fallback(
                    img1_path,
                    img2_path,
                    fallback_runtime_reason,
                )
                mode_results.append(fallback_result)
                return self._build_final_result(fallback_result, mode_results, None, img1_path, img2_path)

            error_text = "; ".join(
                [
                    f"{entry.get('mode')}: {entry.get('error')}"
                    for entry in mode_results
                    if entry.get("error")
                ]
            )
            return self._build_final_result(None, mode_results, f"All similarity modes failed: {error_text}", img1_path, img2_path)

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
