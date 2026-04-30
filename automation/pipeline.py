from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageOps

from automation.config import AutomationConfig
from automation.discovery import CaseRecord, detect_existing_outputs
from automation.manifest import AutomationManifest
from automation.oldcam import run_oldcam
from face_crop_service import extract_portrait_crop
from face_similarity import compute_face_similarity_details
from kling_generator_falai import FalAIKlingGenerator
from outpaint_geometry import compute_percent_expand_plan, compute_provider_caps
from outpaint_generator import OutpaintGenerator
from selfie_generator import SelfieGenerator


ProgressCB = Optional[Callable[[str, str], None]]


@dataclass
class PipelineDeps:
    outpaint_factory: Callable[[], OutpaintGenerator]
    selfie_factory: Callable[[], SelfieGenerator]
    video_factory: Callable[[], FalAIKlingGenerator]


class AutoPipelineRunner:
    def __init__(
        self,
        config: Dict[str, Any],
        automation_config: AutomationConfig,
        manifest: AutomationManifest,
        progress_cb: ProgressCB = None,
        deps: Optional[PipelineDeps] = None,
    ):
        self.config = config
        self.automation = automation_config
        self.manifest = manifest
        self.progress_cb = progress_cb
        self.deps = deps or PipelineDeps(
            outpaint_factory=lambda: OutpaintGenerator(
                api_key=self.config.get("falai_api_key", ""),
                freeimage_key=self.config.get("freeimage_api_key"),
                bfl_api_key=self.config.get("bfl_api_key"),
            ),
            selfie_factory=lambda: SelfieGenerator(
                api_key=self.config.get("falai_api_key", ""),
                freeimage_key=self.config.get("freeimage_api_key"),
                bfl_api_key=self.config.get("bfl_api_key"),
            ),
            video_factory=lambda: FalAIKlingGenerator(
                api_key=self.config.get("falai_api_key", ""),
                verbose=self.config.get("verbose_logging", False),
                model_endpoint=self.config.get("current_model"),
                model_display_name=self.config.get("model_display_name"),
                prompt_slot=int(self.config.get("current_prompt_slot", 1)),
                freeimage_key=self.config.get("freeimage_api_key"),
            ),
        )
        self.last_case_results: Dict[str, Dict[str, Any]] = {}

    def _report(self, message: str, level: str = "info") -> None:
        if self.progress_cb:
            self.progress_cb(message, level)

    def _effective_reprocess_mode(self) -> str:
        if not self.automation.get("automation_allow_reprocess", False):
            return "skip"
        mode = str(self.automation.get("automation_reprocess_mode", "skip")).lower()
        return mode if mode in {"skip", "overwrite", "increment"} else "skip"

    @staticmethod
    def _next_increment_path(path: Path) -> Path:
        if not path.exists():
            return path
        idx = 1
        while True:
            candidate = path.with_name(f"{path.stem}_v{idx}{path.suffix}")
            if not candidate.exists():
                return candidate
            idx += 1

    def _policy_meta(self, step_name: str, reused_existing: bool, mode: str) -> Dict[str, Any]:
        return {
            "reprocess_mode": mode,
            "reused_existing": reused_existing,
            "step": step_name,
        }

    def _set_active_step(self, case_entry: Dict[str, Any], step_name: Optional[str]) -> None:
        case_entry["active_step"] = step_name
        self.manifest.save_atomic()

    def validate_configuration(self) -> List[str]:
        issues: List[str] = []
        if not self.config.get("falai_api_key"):
            issues.append("Missing falai_api_key in config.")
        if self.automation.get("automation_similarity_threshold", 80) < 0 or self.automation.get("automation_similarity_threshold", 80) > 100:
            issues.append("automation_similarity_threshold must be 0..100.")
        if self.automation.get("automation_front_expand_mode") == "percent" and int(self.automation.get("automation_front_expand_percent", 0)) < 0:
            issues.append("automation_front_expand_percent must be >= 0.")
        if self.automation.get("automation_oldcam_required", False) and not self.automation.get("automation_oldcam_enabled", True):
            issues.append("automation_oldcam_required=true requires automation_oldcam_enabled=true.")
        return issues

    def run(self, cases: List[CaseRecord]) -> Dict[str, int]:
        validation_issues = self.validate_configuration()
        if validation_issues:
            raise ValueError("Configuration validation failed: " + "; ".join(validation_issues))

        stats = {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
        for case in cases:
            self.manifest.ensure_case(case.relative_key, case.case_dir, case.front_path)
            if self.automation.get("automation_skip_completed", True) and self.manifest.case_is_complete_and_valid(case.relative_key):
                stats["skipped"] += 1
                self.last_case_results[case.relative_key] = {"status": "skipped", "reason": "already complete"}
                continue
            try:
                final_status = self._run_case(case)
            except Exception as exc:
                self._report(f"[{case.relative_key}] case failed: {exc}", "error")
                active_step = self.manifest.data.get("cases", {}).get(case.relative_key, {}).get("active_step")
                if active_step:
                    try:
                        self.manifest.update_step(case.relative_key, active_step, "failed", error=str(exc))
                    except Exception:
                        pass
                self.manifest.data["cases"][case.relative_key]["status"] = "failed"
                self.manifest.data["cases"][case.relative_key]["active_step"] = None
                self.manifest.save_atomic()
                stats["failed"] += 1
                self.last_case_results[case.relative_key] = {"status": "failed", "reason": str(exc)}
                continue

            stats[final_status] = stats.get(final_status, 0) + 1
            self.last_case_results[case.relative_key] = {"status": final_status, "reason": ""}
        return stats

    def _run_case(self, case: CaseRecord) -> str:
        case_dir = case.case_dir
        case_key = case.relative_key
        existing = detect_existing_outputs(case_dir)
        case_entry = self.manifest.data["cases"][case_key]
        case_entry["status"] = "running"
        self._set_active_step(case_entry, None)
        self.manifest.save_atomic()

        outpaint = self.deps.outpaint_factory()
        outpaint.set_progress_callback(self.progress_cb)
        reprocess_mode = self._effective_reprocess_mode()
        front_provider = str(self.automation.get("automation_front_expand_provider", "auto")).lower()
        selfie_provider = str(self.automation.get("automation_selfie_expand_provider", "auto")).lower()
        resolved_front_provider = "bfl" if front_provider == "bfl" else "fal"
        resolved_selfie_provider = "bfl" if selfie_provider == "bfl" else "fal"
        case_entry["policy"] = {"reprocess_mode": reprocess_mode}
        self.manifest.save_atomic()

        # Step 1: front expand
        front_expanded = existing.front_expanded or (case_dir / self.automation.get("automation_front_output_name", "front-expanded.png"))
        if self.automation.get("automation_front_expand_enabled", True):
            current_step = self.manifest.get_step(case_key, "front_expand")
            existing_front_step_output = current_step.get("output")
            if (
                reprocess_mode == "skip"
                and current_step.get("status") == "complete"
                and existing_front_step_output
                and Path(existing_front_step_output).exists()
            ):
                front_expanded = Path(existing_front_step_output)
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta=self._policy_meta("front_expand", True, reprocess_mode),
                )
            elif reprocess_mode == "skip" and front_expanded and Path(front_expanded).exists():
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta=self._policy_meta("front_expand", True, reprocess_mode),
                )
            else:
                target_output = Path(front_expanded)
                if reprocess_mode == "increment":
                    target_output = self._next_increment_path(target_output)
                front_is_document = self.automation.get("automation_front_expand_mode") == "document_3x4"
                front_expand_kwargs: Dict[str, Any] = {}
                if not front_is_document:
                    pct = int(self.automation.get("automation_front_expand_percent", 30))
                    with Image.open(case.front_path) as _img:
                        width, height = ImageOps.exif_transpose(_img).size
                    plan = compute_percent_expand_plan(
                        width,
                        height,
                        pct,
                        compute_provider_caps(resolved_front_provider),
                    )
                    front_expand_kwargs = {
                        "expand_left": int(plan["left"]),
                        "expand_right": int(plan["right"]),
                        "expand_top": int(plan["top"]),
                        "expand_bottom": int(plan["bottom"]),
                    }
                self._set_active_step(case_entry, "front_expand")
                self.manifest.update_step(case_key, "front_expand", "running")
                result = outpaint.outpaint(
                    image_path=str(case.front_path),
                    output_folder=str(case_dir),
                    output_path=str(target_output),
                    provider=self.automation.get("automation_front_expand_provider", "auto").replace("auto", ""),
                    document_mode=front_is_document,
                    edge_seal_px=int(self.automation.get("automation_front_edge_seal_px", 12))
                    if self.automation.get("automation_front_edge_seal_enabled", True)
                    else 0,
                    **front_expand_kwargs,
                )
                if not result:
                    self.manifest.update_step(case_key, "front_expand", "failed", error="front expansion failed")
                    case_entry["status"] = "failed"
                    self.manifest.save_atomic()
                    return "failed"
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(result),
                    meta=self._policy_meta("front_expand", False, reprocess_mode),
                )
                front_expanded = Path(result)
        else:
            self.manifest.update_step(case_key, "front_expand", "skipped", output=str(front_expanded))

        # Step 2: extract portrait from original front
        extracted_path = case_dir / self.automation.get("automation_extract_output_name", "extracted.png")
        extract_meta: Dict[str, Any]
        extract_step = self.manifest.get_step(case_key, "extract_portrait")
        existing_extract_output = extract_step.get("output")
        if (
            not self.automation.get("automation_extract_enabled", True)
        ):
            self.manifest.update_step(case_key, "extract_portrait", "skipped", output=str(extracted_path))
            extract_meta = {}
        elif (
            reprocess_mode == "skip"
            and extract_step.get("status") == "complete"
            and existing_extract_output
            and Path(existing_extract_output).exists()
        ):
            extracted_path = Path(existing_extract_output)
            extract_meta = extract_step.get("meta") or {}
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={**extract_meta, **self._policy_meta("extract_portrait", True, reprocess_mode)},
            )
        elif reprocess_mode == "skip" and extracted_path.exists():
            extract_meta = extract_step.get("meta") or {}
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={**extract_meta, **self._policy_meta("extract_portrait", True, reprocess_mode)},
            )
        else:
            target_extract_path = extracted_path
            if reprocess_mode == "increment":
                target_extract_path = self._next_increment_path(extracted_path)
            self._set_active_step(case_entry, "extract_portrait")
            self.manifest.update_step(case_key, "extract_portrait", "running")
            extract_meta = extract_portrait_crop(
                input_path=str(case.front_path),
                output_path=str(target_extract_path),
                crop_multiplier=float(self.automation.get("automation_crop_multiplier", 1.5)),
                progress_cb=self.progress_cb,
            )
            extracted_path = target_extract_path
        self.manifest.update_step(
            case_key,
            "extract_portrait",
            "complete",
            output=str(extracted_path),
            meta={
                "confidence": extract_meta.get("confidence"),
                "crop_box": extract_meta.get("crop_box"),
                "extractor": extract_meta.get("extractor"),
                **self._policy_meta("extract_portrait", False, reprocess_mode),
            },
        )

        # Step 3/4: selfie + similarity gate
        selfie_enabled = bool(self.automation.get("automation_selfie_enabled", True))
        if not selfie_enabled:
            self.manifest.update_step(
                case_key,
                "selfie_generate",
                "manual_review",
                error="selfie generation disabled by automation_selfie_enabled=false",
                meta=self._policy_meta("selfie_generate", False, reprocess_mode),
            )
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                error="similarity gate skipped because selfie generation is disabled",
                meta={"threshold": int(self.automation.get("automation_similarity_threshold", 80))},
            )
            case_entry["status"] = "manual_review"
            self.manifest.save_atomic()
            return "manual_review"

        selfie = self.deps.selfie_factory()
        selfie.set_progress_callback(self.progress_cb)
        selfie_folder = case_dir / "gen-images"
        selfie_folder.mkdir(exist_ok=True)
        model_endpoints = list(self.automation.get("automation_selfie_models", ["openai/gpt-image-2/edit"]))
        threshold = int(self.automation.get("automation_similarity_threshold", 80))
        max_attempts = max(1, int(self.automation.get("automation_selfie_max_attempts_per_model", 1)))

        best_path: Optional[str] = str(existing.selfie_candidate) if (
            reprocess_mode == "skip"
            and existing.selfie_candidate
            and self.automation.get("automation_skip_if_selfie_exists", True)
        ) else None
        best_score = -1
        self.manifest.update_step(case_key, "selfie_generate", "running")
        if best_path:
            score_info = compute_face_similarity_details(str(extracted_path), best_path, report_cb=self.progress_cb)
            best_score = int(score_info.get("score", 0))
            self._report(f"[{case_key}] Reused existing selfie: {Path(best_path).name}", "info")
        else:
            for endpoint in model_endpoints:
                for _attempt in range(max_attempts):
                    generated = selfie.generate(
                        image_path=str(extracted_path),
                        prompt=self.config.get("selfie_prompt_template", "portrait selfie"),
                        output_folder=str(selfie_folder),
                        model_endpoint=endpoint,
                    )
                    if not generated:
                        continue
                    score_info = compute_face_similarity_details(str(extracted_path), generated, report_cb=self.progress_cb)
                    score = int(score_info.get("score", 0))
                    if score > best_score:
                        best_score = score
                        best_path = generated
                    if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and score >= threshold:
                        break
                if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and best_score >= threshold:
                    break

        if not best_path:
            self.manifest.update_step(case_key, "selfie_generate", "failed", error="selfie generation failed")
            case_entry["status"] = "failed"
            self.manifest.save_atomic()
            return "failed"
        self.manifest.update_step(
            case_key,
            "selfie_generate",
            "complete",
            output=best_path,
            meta={"best_score": best_score, **self._policy_meta("selfie_generate", bool(existing.selfie_candidate and best_path == str(existing.selfie_candidate)), reprocess_mode)},
        )
        if best_score < threshold:
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=f"similarity {best_score} below threshold {threshold}",
                meta={"score": best_score, "threshold": threshold},
            )
            case_entry["status"] = "manual_review"
            self.manifest.save_atomic()
            return "manual_review"
        self.manifest.update_step(
            case_key,
            "similarity_gate",
            "complete",
            output=best_path,
            meta={"score": best_score, "threshold": threshold},
        )

        # Step 5: selfie expand
        final_still = best_path
        if self.automation.get("automation_selfie_expand_enabled", True):
            pct = int(self.automation.get("automation_selfie_expand_percent", 30))
            with Image.open(best_path) as _img:
                width, height = ImageOps.exif_transpose(_img).size
            plan = compute_percent_expand_plan(
                width,
                height,
                pct,
                compute_provider_caps(resolved_selfie_provider),
            )
            margins = {
                "left": int(plan["left"]),
                "right": int(plan["right"]),
                "top": int(plan["top"]),
                "bottom": int(plan["bottom"]),
            }
            self.manifest.update_step(case_key, "selfie_expand", "running")
            expanded_output = case_dir / "gen-images" / f"{Path(best_path).stem}-expanded.png"
            if reprocess_mode == "increment":
                expanded_output = self._next_increment_path(expanded_output)
            expanded_result = outpaint.outpaint(
                image_path=best_path,
                output_folder=str(case_dir / "gen-images"),
                output_path=str(expanded_output),
                provider=self.automation.get("automation_selfie_expand_provider", "auto").replace("auto", ""),
                document_mode=self.automation.get("automation_selfie_expand_mode") == "centered_3x4",
                expand_left=margins["left"],
                expand_right=margins["right"],
                expand_top=margins["top"],
                expand_bottom=margins["bottom"],
                edge_seal_px=0,
            )
            if expanded_result:
                final_still = expanded_result
                self.manifest.update_step(
                    case_key,
                    "selfie_expand",
                    "complete",
                    output=expanded_result,
                    meta=self._policy_meta("selfie_expand", False, reprocess_mode),
                )
            else:
                self.manifest.update_step(case_key, "selfie_expand", "failed", error="selfie expand failed")
        else:
            self.manifest.update_step(case_key, "selfie_expand", "skipped", output=best_path)

        # Step 6: video generation
        if self.automation.get("automation_video_enabled", True):
            skipped_existing_video = False
            if (
                reprocess_mode == "skip"
                and self.automation.get("automation_skip_if_video_exists", True)
                and existing.video_candidate
            ):
                skipped_existing_video = True
                self.manifest.update_step(
                    case_key,
                    "video_generate",
                    "skipped",
                    output=str(existing.video_candidate),
                    meta=self._policy_meta("video_generate", True, reprocess_mode),
                )
                if not self.automation.get("automation_oldcam_enabled", True):
                    self.manifest.update_step(case_key, "oldcam", "skipped", error="oldcam disabled")
                    case_entry["status"] = "complete"
                    self.manifest.save_atomic()
                    return "completed"
            if not skipped_existing_video:
                video = self.deps.video_factory()
                video.set_progress_callback(self.progress_cb)
                self.manifest.update_step(case_key, "video_generate", "running")
                video_output_dir = case_dir / "gen-videos"
                video_output_dir.mkdir(exist_ok=True)
                output_video = video.create_kling_generation(
                    character_image_path=final_still,
                    output_folder=str(video_output_dir),
                    custom_prompt=self.config.get("saved_prompts", {}).get(str(self.config.get("current_prompt_slot", 1)))
                    if self.automation.get("automation_video_use_existing_prompt", True)
                    else None,
                    duration=int(self.config.get("video_duration", 10)),
                    aspect_ratio=self.automation.get("automation_video_aspect_ratio", "3:4"),
                    resolution=self.config.get("resolution", "720p"),
                    seed=int(self.config.get("seed", -1)),
                    camera_fixed=bool(self.config.get("camera_fixed", False)),
                    generate_audio=bool(self.config.get("generate_audio", False)),
                    use_source_folder=False,
                )
                if not output_video:
                    self.manifest.update_step(case_key, "video_generate", "failed", error="video generation failed")
                    case_entry["status"] = "failed"
                    self.manifest.save_atomic()
                    return "failed"
                if reprocess_mode == "increment":
                    out_video_path = Path(output_video)
                    inc_video_path = self._next_increment_path(out_video_path)
                    if inc_video_path != out_video_path:
                        out_video_path.replace(inc_video_path)
                        output_video = str(inc_video_path)
                self.manifest.update_step(
                    case_key,
                    "video_generate",
                    "complete",
                    output=output_video,
                    meta=self._policy_meta("video_generate", False, reprocess_mode),
                )
        else:
            self.manifest.update_step(case_key, "video_generate", "skipped", output=final_still)

        # Step 7: optional oldcam pass
        if self.automation.get("automation_oldcam_enabled", True):
            selected_video = (
                self.manifest.get_step(case_key, "video_generate").get("output")
                or existing.video_candidate
            )
            if selected_video:
                case_entry["active_step"] = "oldcam"
                self._set_active_step(case_entry, "oldcam")
                self.manifest.update_step(case_key, "oldcam", "running")
                oldcam_output = run_oldcam(
                    video_path=Path(selected_video),
                    version_setting=str(self.automation.get("automation_oldcam_version", "v8")),
                    repo_root=Path(__file__).resolve().parent.parent,
                    progress_cb=self.progress_cb,
                )
                if oldcam_output:
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        "complete",
                        output=str(oldcam_output),
                        meta=self._policy_meta("oldcam", False, reprocess_mode),
                    )
                else:
                    required = bool(self.automation.get("automation_oldcam_required", False))
                    fail_status = "failed" if required else "skipped"
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        fail_status,
                        error="oldcam failed or unavailable",
                        meta={**self._policy_meta("oldcam", False, reprocess_mode), "required": required},
                    )
                    if required:
                        case_entry["status"] = "failed"
                        self.manifest.save_atomic()
                        return "failed"
            else:
                self.manifest.update_step(case_key, "oldcam", "skipped", error="no video for oldcam")
        else:
            self.manifest.update_step(case_key, "oldcam", "skipped", error="oldcam disabled")

        self._set_active_step(case_entry, None)
        case_entry["status"] = "complete"
        self.manifest.save_atomic()
        return "completed"
