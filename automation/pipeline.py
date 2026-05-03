from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageOps

from automation.config import AutomationConfig, DEFAULT_SELFIE_PROMPT
from automation.discovery import CaseRecord, detect_existing_outputs
from automation.logger import build_safe_config_snapshot, create_automation_logger
from automation.manifest import AutomationManifest, now_iso
from automation.oldcam import discover_oldcam_versions, ensure_oldcam_dependencies, run_oldcam
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
        self.logger, self.log_path = create_automation_logger(self.config, self.config.get("automation_root_folder"))
        self.verbose_logging = bool(self.config.get("automation_verbose_logging", self.config.get("verbose_logging", True)))

    def _report(self, message: str, level: str = "info") -> None:
        if self.progress_cb:
            self.progress_cb(message, level)
        if self.verbose_logging:
            if level == "error":
                self.logger.error(message)
            elif level == "warning":
                self.logger.warning(message)
            else:
                self.logger.info(message)

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

    def _resolve_outpaint_provider(self, configured_provider: str) -> str:
        normalized = str(configured_provider or "auto").strip().lower()
        if normalized in {"bfl", "fal"}:
            return normalized
        if self.config.get("bfl_api_key"):
            return "bfl"
        return "fal"

    def resolve_provider_summary(self) -> Dict[str, str]:
        front_configured = str(self.automation.get("automation_front_expand_provider", "auto")).lower()
        selfie_configured = str(self.automation.get("automation_selfie_expand_provider", "auto")).lower()
        return {
            "front_configured": front_configured,
            "front_resolved": self._resolve_outpaint_provider(front_configured),
            "selfie_configured": selfie_configured,
            "selfie_resolved": self._resolve_outpaint_provider(selfie_configured),
        }

    def resolve_selfie_prompt(self) -> Dict[str, Any]:
        slot = str(self.automation.get("automation_selfie_prompt_slot", 1))
        prompts = self.automation.get("automation_selfie_prompts", {}) or {}
        prompt = str(prompts.get(slot, "") or "").strip()
        if prompt:
            source = f"slot:{slot}"
        else:
            prompt = DEFAULT_SELFIE_PROMPT
            source = "default_seeded_prompt"
        return {"slot": slot, "prompt": prompt, "source": source}

    def _finalize_case(self, case_entry: Dict[str, Any], final_status: str) -> str:
        status_value = "complete" if final_status == "completed" else final_status
        case_entry["status"] = status_value
        self._set_active_step(case_entry, None)
        self.manifest.save_atomic()
        return final_status

    def validate_configuration(self) -> List[str]:
        issues: List[str] = []
        fal_key = str(self.config.get("falai_api_key", "")).strip()
        bfl_key = str(self.config.get("bfl_api_key", "")).strip()
        video_enabled = bool(self.automation.get("automation_video_enabled", True))
        selfie_enabled = bool(self.automation.get("automation_selfie_enabled", True))
        front_expand_enabled = bool(self.automation.get("automation_front_expand_enabled", True))
        selfie_expand_enabled = bool(self.automation.get("automation_selfie_expand_enabled", True))
        provider_summary = self.resolve_provider_summary()
        front_provider = provider_summary["front_configured"]
        selfie_provider = provider_summary["selfie_configured"]

        if video_enabled and not fal_key:
            issues.append("Missing falai_api_key in config (required for Kling video step).")
        if selfie_enabled and not fal_key:
            issues.append("Missing falai_api_key in config (required for selfie generation).")

        if front_expand_enabled:
            if front_provider == "fal" and not fal_key:
                issues.append("Missing falai_api_key for front expand provider=fal.")
            if front_provider == "bfl" and not bfl_key:
                issues.append("Missing bfl_api_key for front expand provider=bfl.")
            if front_provider == "auto" and not fal_key and not bfl_key:
                issues.append("Missing falai_api_key/bfl_api_key for front expand provider=auto.")

        if selfie_expand_enabled:
            if selfie_provider == "fal" and not fal_key:
                issues.append("Missing falai_api_key for selfie expand provider=fal.")
            if selfie_provider == "bfl" and not bfl_key:
                issues.append("Missing bfl_api_key for selfie expand provider=bfl.")
            if selfie_provider == "auto" and not fal_key and not bfl_key:
                issues.append("Missing falai_api_key/bfl_api_key for selfie expand provider=auto.")

        if self.automation.get("automation_similarity_threshold", 80) < 0 or self.automation.get("automation_similarity_threshold", 80) > 100:
            issues.append("automation_similarity_threshold must be 0..100.")
        if self.automation.get("automation_front_expand_mode") == "percent" and int(self.automation.get("automation_front_expand_percent", 0)) < 0:
            issues.append("automation_front_expand_percent must be >= 0.")
        if self.automation.get("automation_oldcam_required", False) and not self.automation.get("automation_oldcam_enabled", True):
            issues.append("automation_oldcam_required=true requires automation_oldcam_enabled=true.")
        if self.automation.get("automation_oldcam_required", False):
            repo_root = Path(__file__).resolve().parent.parent
            versions = discover_oldcam_versions(repo_root)
            deps_ok, deps_error = ensure_oldcam_dependencies()
            configured_version = str(self.automation.get("automation_oldcam_version", "all")).lower()
            if configured_version == "all":
                expected_versions = {"v7", "v8"}
            else:
                expected_versions = {configured_version}
            available_versions = {str(v).lower() for v in versions}
            missing_versions = sorted(expected_versions.difference(available_versions))
            if missing_versions:
                issues.append(
                    f"Oldcam required but missing version(s): {', '.join(missing_versions)}. Available: {', '.join(sorted(available_versions)) or '(none)'}."
                )
            if not deps_ok:
                issues.append(f"Oldcam required but dependencies are not ready: {deps_error or 'unknown dependency error'}.")
        return issues

    def run(self, cases: List[CaseRecord]) -> Dict[str, int]:
        self.logger.info("automation run start")
        self.logger.info("automation config snapshot: %s", build_safe_config_snapshot(self.config, self.config.get("automation_root_folder")))
        self.logger.info("provider summary: %s", self.resolve_provider_summary())
        self.logger.info(
            "selection summary: selfie_models=%s selfie_prompt_slot=%s video_model=%s kling_prompt_slot=%s",
            self.automation.get("automation_selfie_models"),
            self.automation.get("automation_selfie_prompt_slot", 1),
            self.config.get("model_display_name") or self.config.get("current_model"),
            self.config.get("current_prompt_slot", 1),
        )
        validation_issues = self.validate_configuration()
        if validation_issues:
            self.logger.error("automation validation failed: %s", validation_issues)
            raise ValueError("Configuration validation failed: " + "; ".join(validation_issues))

        stats = {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
        for case in cases:
            self.logger.info("case start: %s", case.relative_key)
            self.manifest.ensure_case(case.relative_key, case.case_dir, case.front_path)
            if self.automation.get("automation_skip_completed", True) and self.manifest.case_is_complete_and_valid(case.relative_key):
                stats["skipped"] += 1
                self.last_case_results[case.relative_key] = {"status": "skipped", "reason": "already complete"}
                self.logger.info("case skipped complete: %s", case.relative_key)
                continue
            try:
                final_status = self._run_case(case)
            except Exception as exc:
                self._report(f"[{case.relative_key}] case failed: {exc}", "error")
                self.logger.exception("case failed with exception: %s", case.relative_key)
                cases_map = self.manifest.data.setdefault("cases", {})
                case_state = cases_map.setdefault(case.relative_key, {})
                active_step = case_state.get("active_step")
                if active_step:
                    try:
                        self.manifest.update_step(case.relative_key, active_step, "failed", error=str(exc))
                    except (OSError, IOError) as update_error:
                        self.logger.exception(
                            "failed to mark step as failed for case=%s step=%s: %s",
                            case.relative_key,
                            active_step,
                            update_error,
                        )
                else:
                    case_state.setdefault("errors", []).append(
                        {"step": "run", "error": str(exc), "at": now_iso()}
                    )
                case_state["status"] = "failed"
                case_state["active_step"] = None
                case_state["updated_at"] = now_iso()
                self.manifest.save_atomic()
                stats["failed"] += 1
                self.last_case_results[case.relative_key] = {"status": "failed", "reason": str(exc)}
                continue

            stats[final_status] = stats.get(final_status, 0) + 1
            existing_result = self.last_case_results.get(case.relative_key, {})
            self.last_case_results[case.relative_key] = {
                "status": final_status,
                "reason": str(existing_result.get("reason", "")),
            }
            self.logger.info("case end: %s status=%s", case.relative_key, final_status)
        self.logger.info("automation run complete stats=%s", stats)
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
        resolved_front_provider = self._resolve_outpaint_provider(front_provider)
        resolved_selfie_provider = self._resolve_outpaint_provider(selfie_provider)
        case_entry["policy"] = {
            "reprocess_mode": reprocess_mode,
            "front_provider_configured": front_provider,
            "front_provider_resolved": resolved_front_provider,
            "selfie_provider_configured": selfie_provider,
            "selfie_provider_resolved": resolved_selfie_provider,
        }
        self.manifest.save_atomic()
        self.logger.info(
            "case %s providers front=%s->%s selfie=%s->%s",
            case_key,
            front_provider,
            resolved_front_provider,
            selfie_provider,
            resolved_selfie_provider,
        )

        # Step 1: front expand
        front_expanded = existing.front_expanded or (case_dir / self.automation.get("automation_front_output_name", "front-expanded.png"))
        configured_front_passes = int(self.automation.get("automation_front_expand_passes", 2))
        front_passes = configured_front_passes if configured_front_passes in {1, 2} else 2
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
                    meta={
                        **self._policy_meta("front_expand", True, reprocess_mode),
                        "configured_passes": front_passes,
                        "executed_passes": 0,
                    },
                )
            elif reprocess_mode == "skip" and front_expanded and Path(front_expanded).exists():
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta={
                        **self._policy_meta("front_expand", True, reprocess_mode),
                        "configured_passes": front_passes,
                        "executed_passes": 0,
                    },
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
                    self.logger.info("case %s front expand geometry width=%s height=%s pct=%s plan=%s", case_key, width, height, pct, plan)
                self._set_active_step(case_entry, "front_expand")
                self.manifest.update_step(case_key, "front_expand", "running")
                result = None
                front_input_path = str(case.front_path)
                executed_passes = 0
                for pass_index in range(front_passes):
                    pass_output = str(target_output) if pass_index == front_passes - 1 else None
                    result = outpaint.outpaint(
                        image_path=front_input_path,
                        output_folder=str(case_dir),
                        output_path=pass_output,
                        provider=resolved_front_provider,
                        document_mode=front_is_document,
                        edge_seal_px=int(self.automation.get("automation_front_edge_seal_px", 12))
                        if self.automation.get("automation_front_edge_seal_enabled", True)
                        else 0,
                        **front_expand_kwargs,
                    )
                    if not result:
                        self.manifest.update_step(
                            case_key,
                            "front_expand",
                            "failed",
                            error=f"front expansion failed on pass {pass_index + 1}",
                            meta={
                                "configured_passes": front_passes,
                                "executed_passes": executed_passes,
                            },
                        )
                        return self._finalize_case(case_entry, "failed")
                    front_input_path = result
                    executed_passes += 1
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(result),
                    meta={
                        **self._policy_meta("front_expand", False, reprocess_mode),
                        "configured_passes": front_passes,
                        "executed_passes": executed_passes,
                    },
                )
                front_expanded = Path(result)
        else:
            self.manifest.update_step(case_key, "front_expand", "skipped", output=str(front_expanded))

        # Step 2: extract portrait from original front
        extracted_path = case_dir / self.automation.get("automation_extract_output_name", "extracted.png")
        extract_meta: Dict[str, Any]
        extract_step = self.manifest.get_step(case_key, "extract_portrait")
        existing_extract_output = extract_step.get("output")
        extraction_skipped = False
        extraction_reused = False
        if (
            not self.automation.get("automation_extract_enabled", True)
        ):
            extraction_skipped = True
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
            extraction_reused = True
        elif reprocess_mode == "skip" and extracted_path.exists():
            extract_meta = extract_step.get("meta") or {}
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={**extract_meta, **self._policy_meta("extract_portrait", True, reprocess_mode)},
            )
            extraction_reused = True
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
        if extraction_skipped:
            if not extracted_path.exists():
                self.manifest.update_step(
                    case_key,
                    "similarity_gate",
                    "manual_review",
                    error="portrait extraction disabled and extracted image missing",
                    meta={"extracted_path": str(extracted_path)},
                )
                return self._finalize_case(case_entry, "manual_review")
        else:
            self.manifest.update_step(
                case_key,
                "extract_portrait",
                "complete",
                output=str(extracted_path),
                meta={
                    "confidence": extract_meta.get("confidence"),
                    "crop_box": extract_meta.get("crop_box"),
                    "extractor": extract_meta.get("extractor"),
                    **self._policy_meta("extract_portrait", extraction_reused, reprocess_mode),
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
            return self._finalize_case(case_entry, "manual_review")

        selfie = self.deps.selfie_factory()
        selfie.set_progress_callback(self.progress_cb)
        selfie_folder = case_dir / "gen-images"
        selfie_folder.mkdir(exist_ok=True)
        model_endpoints = list(self.automation.get("automation_selfie_models", ["fal-ai/nano-banana-2/edit"]))
        selfie_prompt_ctx = self.resolve_selfie_prompt()
        threshold = int(self.automation.get("automation_similarity_threshold", 80))
        max_attempts = max(1, int(self.automation.get("automation_selfie_max_attempts_per_model", 1)))
        self.logger.info(
            "case %s selfie config models=%s prompt_slot=%s prompt_source=%s threshold=%s",
            case_key,
            model_endpoints,
            selfie_prompt_ctx["slot"],
            selfie_prompt_ctx["source"],
            threshold,
        )

        best_path: Optional[str] = str(existing.selfie_candidate) if (
            reprocess_mode == "skip"
            and existing.selfie_candidate
            and self.automation.get("automation_skip_if_selfie_exists", True)
        ) else None
        best_score = -1
        best_similarity_meta: Dict[str, Any] = {"score": None, "threshold": threshold, "match": None, "error": None}
        self._set_active_step(case_entry, "selfie_generate")
        self.manifest.update_step(case_key, "selfie_generate", "running")
        if best_path:
            score_info = compute_face_similarity_details(str(extracted_path), best_path, report_cb=self.progress_cb)
            best_score = int(score_info.get("score", 0))
            best_similarity_meta = {
                "score": score_info.get("score"),
                "threshold": threshold,
                "match": score_info.get("match"),
                "error": score_info.get("error"),
            }
            self._report(f"[{case_key}] Reused existing selfie: {Path(best_path).name}", "info")
        else:
            for endpoint in model_endpoints:
                for _attempt in range(max_attempts):
                    generated = selfie.generate(
                        image_path=str(extracted_path),
                        prompt=selfie_prompt_ctx["prompt"],
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
                        best_similarity_meta = {
                            "score": score_info.get("score"),
                            "threshold": threshold,
                            "match": score_info.get("match"),
                            "error": score_info.get("error"),
                        }
                    if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and score >= threshold:
                        break
                if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and best_score >= threshold:
                    break

        if not best_path:
            self.manifest.update_step(case_key, "selfie_generate", "failed", error="selfie generation failed")
            return self._finalize_case(case_entry, "failed")
        self.manifest.update_step(
            case_key,
            "selfie_generate",
            "complete",
            output=best_path,
            meta={
                "best_score": best_score,
                "selfie_prompt_slot": selfie_prompt_ctx["slot"],
                "selfie_prompt_source": selfie_prompt_ctx["source"],
                **self._policy_meta("selfie_generate", bool(existing.selfie_candidate and best_path == str(existing.selfie_candidate)), reprocess_mode),
            },
        )
        self.logger.info("case %s similarity details=%s", case_key, best_similarity_meta)
        if best_similarity_meta.get("error"):
            similarity_error = str(best_similarity_meta.get("error"))
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=f"similarity unavailable: {similarity_error}",
                meta=best_similarity_meta,
            )
            self.last_case_results[case_key] = {"status": "manual_review", "reason": f"similarity unavailable: {similarity_error}"}
            self.logger.warning("case %s similarity unavailable: %s", case_key, similarity_error)
            return self._finalize_case(case_entry, "manual_review")
        if best_score < threshold:
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=f"similarity {best_score} below threshold {threshold}",
                meta=best_similarity_meta,
            )
            self.last_case_results[case_key] = {"status": "manual_review", "reason": f"similarity {best_score} below threshold {threshold}"}
            return self._finalize_case(case_entry, "manual_review")
        self.manifest.update_step(
            case_key,
            "similarity_gate",
            "complete",
            output=best_path,
            meta=best_similarity_meta,
        )

        # Step 5: selfie expand
        final_still = best_path
        if self.automation.get("automation_selfie_expand_enabled", True):
            selfie_expand_step = self.manifest.get_step(case_key, "selfie_expand")
            selfie_expand_output = selfie_expand_step.get("output")
            if (
                reprocess_mode == "skip"
                and selfie_expand_step.get("status") == "complete"
                and selfie_expand_output
                and Path(selfie_expand_output).exists()
            ):
                final_still = selfie_expand_output
                self.manifest.update_step(
                    case_key,
                    "selfie_expand",
                    "complete",
                    output=selfie_expand_output,
                    meta=self._policy_meta("selfie_expand", True, reprocess_mode),
                )
            else:
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
                self.logger.info("case %s selfie expand geometry width=%s height=%s pct=%s plan=%s", case_key, width, height, pct, plan)
                self._set_active_step(case_entry, "selfie_expand")
                self.manifest.update_step(case_key, "selfie_expand", "running")
                expanded_output = case_dir / "gen-images" / f"{Path(best_path).stem}-expanded.png"
                if reprocess_mode == "increment":
                    expanded_output = self._next_increment_path(expanded_output)
                expanded_result = outpaint.outpaint(
                    image_path=best_path,
                    output_folder=str(case_dir / "gen-images"),
                    output_path=str(expanded_output),
                    provider=resolved_selfie_provider,
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
                    return self._finalize_case(case_entry, "failed")
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
                    return self._finalize_case(case_entry, "completed")
            if not skipped_existing_video:
                video = self.deps.video_factory()
                video.set_progress_callback(self.progress_cb)
                self._set_active_step(case_entry, "video_generate")
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
                    return self._finalize_case(case_entry, "failed")
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
            self.manifest.update_step(case_key, "video_generate", "skipped", output=None)

        # Step 7: optional oldcam pass
        if self.automation.get("automation_oldcam_enabled", True):
            manifest_video = self.manifest.get_step(case_key, "video_generate").get("output")
            manifest_video_path = Path(manifest_video) if manifest_video else None
            if (
                manifest_video_path
                and manifest_video_path.exists()
                and manifest_video_path.suffix.lower() == ".mp4"
            ):
                selected_video_path = manifest_video_path
            elif existing.video_candidate:
                selected_video_path = Path(existing.video_candidate)
            else:
                selected_video_path = None
            if selected_video_path and selected_video_path.exists() and selected_video_path.suffix.lower() == ".mp4":
                self.logger.info("case %s oldcam readiness=ready version=%s required=%s", case_key, self.automation.get("automation_oldcam_version", "v8"), bool(self.automation.get("automation_oldcam_required", False)))
                self._set_active_step(case_entry, "oldcam")
                self.manifest.update_step(case_key, "oldcam", "running")
                oldcam_output = run_oldcam(
                    video_path=selected_video_path,
                    version_setting=str(self.automation.get("automation_oldcam_version", "v8")),
                    repo_root=Path(__file__).resolve().parent.parent,
                    progress_cb=self.progress_cb,
                )
                if oldcam_output:
                    self.logger.info("case %s oldcam output=%s", case_key, oldcam_output)
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        "complete",
                        output=str(oldcam_output),
                        meta=self._policy_meta("oldcam", False, reprocess_mode),
                    )
                else:
                    required = bool(self.automation.get("automation_oldcam_required", False))
                    self.logger.warning("case %s oldcam failed required=%s", case_key, required)
                    fail_status = "failed" if required else "skipped"
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        fail_status,
                        error="oldcam failed or unavailable",
                        meta={**self._policy_meta("oldcam", False, reprocess_mode), "required": required},
                    )
                    if required:
                        return self._finalize_case(case_entry, "failed")
            else:
                required = bool(self.automation.get("automation_oldcam_required", False))
                self.logger.warning("case %s oldcam readiness=not-ready required=%s", case_key, required)
                reason = "missing or non-mp4 video for oldcam"
                self.manifest.update_step(
                    case_key,
                    "oldcam",
                    "failed" if required else "skipped",
                    error=reason,
                    meta={"required": required},
                )
                if required:
                    return self._finalize_case(case_entry, "failed")
        else:
            self.manifest.update_step(case_key, "oldcam", "skipped", error="oldcam disabled")

        return self._finalize_case(case_entry, "completed")
