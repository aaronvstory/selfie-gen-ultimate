from __future__ import annotations

from dataclasses import dataclass
import gc
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image, ImageOps

from automation.config import (
    AutomationConfig,
    DEFAULT_SELFIE_PROMPT,
    get_outpaint_fal_timeout_seconds,
    resolve_cli_kling_prompt_slot,
    resolve_cli_video_duration,
    resolve_cli_video_model,
)
from automation.discovery import CaseRecord, detect_existing_outputs
from automation.logger import build_safe_config_snapshot, create_automation_logger
from automation.manifest import AutomationManifest, now_iso
from automation.oldcam import (
    _version_key as _oldcam_version_key,
    build_oldcam_output_path,
    discover_oldcam_versions,
    ensure_oldcam_dependencies,
    normalize_oldcam_versions,
    run_oldcam_all,
    run_oldcam_version,
)
from automation.rppg import is_rppg_artifact, run_rppg
from automation.video_loop import check_ffmpeg_available, create_looped_video
from automation.video_crush import CRUSH_RESOLUTIONS, crush_suffix, crush_video
from automation.video_aa import AA_PIPELINES, aa_suffix, check_aa_available, run_aa
from face_crop_service import extract_portrait_crop
from face_similarity import compute_face_similarity_details
from kling_generator_falai import FalAIKlingGenerator
from outpaint_geometry import compute_percent_expand_plan, compute_provider_caps
from outpaint_generator import OutpaintGenerator
from selfie_generator import SelfieGenerator


ProgressCB = Optional[Callable[[str, str], None]]


def _with_outpaint_detail(base_message: str, outpaint: "OutpaintGenerator") -> str:
    """Append the generator's real last-error detail to a case error message.

    The outpaint generator captures the actual provider failure (e.g. a fal
    HTTP 422 ``image_url: Failed to generate outpainted image: ...``) in
    ``get_last_outpaint_error_detail``. Surfacing it here means the case
    summary / live "Issue" line shows the real cause instead of the opaque
    "selfie expand failed". Best-effort + defensive: stub generators in tests
    may not implement the getter."""
    detail = ""
    try:
        getter = getattr(outpaint, "get_last_outpaint_error_detail", None)
        if callable(getter):
            detail = (getter() or "").strip()
    except Exception:
        detail = ""
    if not detail:
        return base_message
    # Avoid a redundant "(reason=fal_failed_or_timed_out)"-only tail adding no
    # information beyond the base message.
    return f"{base_message}: {detail}"


@dataclass
class PipelineDeps:
    outpaint_factory: Callable[[], OutpaintGenerator]
    selfie_factory: Callable[[], SelfieGenerator]
    video_factory: Callable[[], FalAIKlingGenerator]


class AutomationAbort(Exception):
    """Raised between pipeline steps when the runner's abort_event is set.

    Caught in run(): the in-flight case's status reverts to "pending" so a
    later Run/Resume picks it up at its first incomplete step (the manifest
    keeps every completed step's state)."""


class AutoPipelineRunner:
    _DEFAULT_OUTPAINT_COMPOSITE_MODE = "preserve_seamless"
    _VALID_OUTPAINT_COMPOSITE_MODES = {"preserve_seamless", "feathered", "hard", "none", "black_fill"}

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
            # Per-surface split (2026-06-11): the CLI's own model/slot keys
            # win; pre-split configs fall back to the shared GUI keys inside
            # the resolvers.
            video_factory=lambda: FalAIKlingGenerator(
                api_key=self.config.get("falai_api_key", ""),
                verbose=self.config.get("verbose_logging", False),
                model_endpoint=resolve_cli_video_model(self.config)[0],
                model_display_name=resolve_cli_video_model(self.config)[1],
                prompt_slot=resolve_cli_kling_prompt_slot(self.config),
                freeimage_key=self.config.get("freeimage_api_key"),
            ),
        )
        self.last_case_results: Dict[str, Dict[str, Any]] = {}
        self.logger, self.log_path = create_automation_logger(self.config, self.config.get("automation_root_folder"))
        self.verbose_logging = bool(self.config.get("automation_verbose_logging", self.config.get("verbose_logging", True)))
        # Cooperative pause/abort (2026-06-11, CLI live-dashboard keys):
        #   pause_event — finish the CURRENT case, then stop (between-cases
        #     check in run()).
        #   abort_event — stop after the CURRENT step (checked at every
        #     _set_active_step transition); the in-flight case reverts to
        #     "pending" so Run/Resume continues it from the first incomplete
        #     step.
        self.pause_event = threading.Event()
        self.abort_event = threading.Event()
        self.stopped_reason: Optional[str] = None
        # Cases whose recorded front image changed this run: every existing
        # output came from the OLD source, so file-based reuse (skip mode +
        # skip_if_*_exists) must not re-adopt them (codex P1, PR #96 round 9).
        self._force_regen_cases: set = set()

    def _read_int(
        self,
        key: str,
        default: int,
        issues: Optional[List[str]] = None,
        *,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
    ) -> int:
        raw = self.automation.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            if issues is not None:
                issues.append(f"{key} must be an integer.")
            return default
        if min_value is not None and value < min_value:
            if issues is not None:
                issues.append(f"{key} must be >= {min_value}.")
            return default
        if max_value is not None and value > max_value:
            if issues is not None:
                issues.append(f"{key} must be <= {max_value}.")
            return default
        return value

    def _read_float(
        self,
        key: str,
        default: float,
        issues: Optional[List[str]] = None,
        *,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
    ) -> float:
        raw = self.automation.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            if issues is not None:
                issues.append(f"{key} must be a number.")
            return default
        if not math.isfinite(value):
            if issues is not None:
                issues.append(f"{key} must be a finite number.")
            return default
        if min_value is not None and value < min_value:
            if issues is not None:
                issues.append(f"{key} must be >= {min_value}.")
            return default
        if max_value is not None and value > max_value:
            if issues is not None:
                issues.append(f"{key} must be <= {max_value}.")
            return default
        return value

    def _read_bool(self, key: str, default: bool) -> bool:
        """Read an automation_* config flag as a strict bool.

        Reuses face_similarity._parse_bool (already the canonical helper for
        config-string-to-bool coercion) so a JSON value of "false" is treated
        as False, not True. Raw bool(...) on a non-empty string returns True
        — that's the bug coderabbit flagged on PR #19 for
        automation_similarity_require_fas_pass, which would have silently
        enabled strict spoof gating whenever the user typed "false" in the
        config file.
        """
        from face_similarity import _parse_bool
        raw = self.automation.get(key, default)
        parsed = _parse_bool(raw)
        return parsed if parsed is not None else bool(default)

    def _oldcam_versions(self) -> List[str]:
        """Canonical (normalized list) oldcam selection from config."""
        return normalize_oldcam_versions(self.automation.get("automation_oldcam_version", []))

    def _crush_resolutions(self) -> List[str]:
        """Canonical crush-resolution labels from config.

        Robust whether or not the config went through
        merge_automation_defaults: re-applies normalize_crush_resolutions so a
        raw config carrying only the legacy ``automation_crush_enabled``
        boolean still resolves correctly (True → ['480p'], absent → ['720p']).
        """
        from automation.video_crush import normalize_crush_resolutions

        # ``self.automation`` is an AutomationConfig (``.get`` but no ``in``).
        # A merged config always carries ``automation_crush_resolutions``; a
        # raw one returns None here, which normalize_crush_resolutions treats
        # as "unset" and falls back to the legacy boolean (also None → 720p
        # default), so both shapes resolve correctly.
        return normalize_crush_resolutions(
            self.automation.get("automation_crush_resolutions"),
            legacy_enabled=self.automation.get("automation_crush_enabled"),
        )

    def _aa_attacks(self) -> List[str]:
        """Canonical AA attack-pipeline labels from config.

        AA is opt-in (default OFF / empty list), so — unlike crush — we must
        NOT let ``normalize_aa_attacks``'s bare default (['prime']) leak in when
        BOTH keys are absent. A merged config always carries
        ``automation_aa_attacks`` (``[]`` when off). Only pass through normalize
        when at least one source key is genuinely present; otherwise return [].
        """
        from automation.video_aa import normalize_aa_attacks

        attacks = self.automation.get("automation_aa_attacks")
        legacy = self.automation.get("automation_aa_enabled")
        if attacks is None and legacy is None:
            return []
        kwargs = {}
        if attacks is not None:
            kwargs["attacks"] = attacks
        if legacy is not None:
            kwargs["legacy_enabled"] = legacy
        return normalize_aa_attacks(**kwargs)

    def _oldcam_active(self) -> bool:
        """Oldcam runs only when enabled AND at least one version is selected.

        Multi-select (2026-06-11) made the empty selection ``[]`` a first-class
        "off" state alongside ``automation_oldcam_enabled=False`` — the UI keeps
        the two coherent but the pipeline must tolerate any combination.
        """
        return bool(self.automation.get("automation_oldcam_enabled", True)) and bool(self._oldcam_versions())

    def _oldcam_inactive_reason(self) -> str:
        if not self.automation.get("automation_oldcam_enabled", True):
            return "oldcam disabled"
        return "no oldcam versions selected"

    def _generate_video_for_still(self, video, still_path: str, video_output_dir: Path) -> Optional[str]:
        """Run one Kling generation for ``still_path`` with the exact kwargs
        the primary Step 6 has always used (CLI parity with the GUI queue —
        see the comment block at the call site). Shared by the primary chain
        and the multi-model fan-out branches so their generations can never
        drift."""
        _slot = str(resolve_cli_kling_prompt_slot(self.config))
        # _read_bool (not raw .get) so a string "false" in the
        # automation config disables prompt/negative reuse as the
        # user intended — raw bool("false") is truthy (CodeRabbit,
        # PR #41). It IS an automation_* key, so self._read_bool's
        # self.automation source is correct here.
        _use_existing = self._read_bool("automation_video_use_existing_prompt", True)
        try:
            _cfg_val = float(self.config.get("cfg_scale_value", 0.7))
        except (TypeError, ValueError):
            _cfg_val = 0.7
        # lock_end_frame is a GUI (kling_config) key, NOT an
        # automation_* key — read it from self.config via the
        # canonical _parse_bool (NOT self._read_bool, which only
        # looks in self.automation and would always return the
        # default here).
        from face_similarity import _parse_bool as _pb
        _lock_ef = _pb(self.config.get("lock_end_frame", True))
        if _lock_ef is None:
            _lock_ef = True
        # A hand-edited null/"" seed must degrade to -1 (random), not crash
        # the whole pipeline at dispatch (Gemini MED, PR #96 round 2).
        try:
            _seed_val = int(self.config.get("seed", -1))
        except (TypeError, ValueError):
            _seed_val = -1
        return video.create_kling_generation(
            character_image_path=still_path,
            output_folder=str(video_output_dir),
            custom_prompt=self.config.get("saved_prompts", {}).get(_slot)
            if _use_existing
            else None,
            negative_prompt=(
                self.config.get("negative_prompts", {}).get(_slot) or None
            )
            if _use_existing
            else None,
            duration=resolve_cli_video_duration(self.config),
            aspect_ratio=self.automation.get("automation_video_aspect_ratio", "3:4"),
            resolution=self.config.get("resolution", "720p"),
            seed=_seed_val,
            camera_fixed=bool(self.config.get("camera_fixed", False)),
            generate_audio=bool(self.config.get("generate_audio", False)),
            cfg_scale=max(0.0, min(1.0, _cfg_val)),
            lock_end_frame=bool(_lock_ef),
            use_source_folder=False,
        )

    def _reset_case_if_front_changed(self, case: CaseRecord) -> None:
        """Reset a case whose FRONT IMAGE re-selected to a different file.

        front_names/front_globs are deliberately excluded from the manifest
        fingerprint (run-scope), but a pattern change can pick a DIFFERENT
        file inside the same folder — and every output recorded for the case
        came from the old source image. Skipping ("already complete") or
        per-step resuming would then silently deliver wrong-source results.
        Captured BEFORE ensure_case, which clobbers the recorded front_path
        (adversarial review M1, PR #96 round 7)."""
        prior_front: Optional[str] = None
        with self.manifest.lock:
            prior_entry = (self.manifest.data.get("cases") or {}).get(case.relative_key)
            if isinstance(prior_entry, dict):
                prior_front = prior_entry.get("front_path")
        if not prior_front or str(prior_front) == str(case.front_path):
            return
        self._report(
            f"[{case.relative_key}] front image changed since last run "
            f"({Path(str(prior_front)).name} -> {Path(str(case.front_path)).name}) — reprocessing from scratch.",
            "warning",
        )
        self.logger.info(
            "case front changed (%s -> %s): %s", prior_front, case.front_path, case.relative_key
        )
        self.manifest.reset_case_for_new_front(case.relative_key, case.case_dir, case.front_path)
        # The manifest reset alone is not enough: file-based reuse would
        # re-adopt the on-disk selfie/video made from the OLD front. Force a
        # real regeneration for this case (codex P1, PR #96 round 9).
        self._force_regen_cases.add(case.relative_key)

    def _branch_run_rppg(self, video_path: Path) -> Optional[Path]:
        """One rPPG injection with the canonical knob set (shared by the
        branch chain; the primary pre-pass keeps its inline call for
        manifest bookkeeping)."""
        return run_rppg(
            video_path=video_path,
            repo_root=Path(__file__).resolve().parent.parent,
            progress_cb=self.progress_cb,
            keep_metrics=self._read_bool("automation_rppg_metrics_in_filename", False),
            iterative=str(self.automation.get("automation_rppg_mode") or "iterative").strip().lower() == "iterative",
            iterate_from_baseline=self._read_bool("automation_rppg_iterate_from_baseline", True),
            skip_diagnosis=self._read_bool("automation_rppg_skip_diagnosis", True),
            skip_kinematic_gate=self._read_bool("automation_rppg_skip_kinematic_gate", True),
            landmark_stride=self._read_int("automation_rppg_landmark_stride", 1, min_value=1),
            verbose=self.verbose_logging,
        )

    @staticmethod
    def _branch_slug(endpoint: str) -> str:
        """Filename-safe model slug for a fan-out branch (single source of
        truth: SelfieGenerator's output-filename slugger)."""
        from selfie_generator import SelfieGenerator
        return SelfieGenerator._model_short_name(endpoint)

    def _existing_branch_candidate(
        self, case_dir: Path, endpoint: str, threshold: int
    ) -> Optional[Dict[str, Any]]:
        """Resume support for fan-out branches: when this run REUSED an
        existing primary selfie (so no per-model generation happened), find
        the best already-on-disk selfie for ``endpoint`` by its slug +
        embedded similarity score (``..._{slug}_sim{NN}_...``). Returns the
        same candidate shape as branch_best entries, or None."""
        slug = self._branch_slug(endpoint)
        if not slug:
            return None
        gen_images = case_dir / "gen-images"
        try:
            # EAFP: missing dir AND restricted-filesystem errors both mean
            # "no resumable branch candidate" — the is_dir preflight itself
            # can raise on odd mounts (Gemini MED, PR #96 round 10).
            candidates = list(gen_images.iterdir())
        except OSError:
            return None
        best: Optional[Dict[str, Any]] = None
        pattern = re.compile(re.escape(slug) + r"_sim(\d+)_", re.IGNORECASE)
        for candidate in candidates:
            if not candidate.is_file() or candidate.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            if "-expanded" in candidate.stem:
                continue  # expansion outputs are downstream artifacts, not selfies
            match = pattern.search(candidate.name)
            if not match:
                continue
            score = int(match.group(1))
            if best is None or score > best["score"]:
                best = {
                    "endpoint": endpoint,
                    "path": str(candidate),
                    "score": score,
                    "similarity": {"score": score, "threshold": threshold, "match": score >= threshold},
                }
        return best

    def _run_extra_branches(
        self,
        *,
        case_key: str,
        case_dir: Path,
        extra_branches: List[Dict[str, Any]],
        reprocess_mode: str,
        resolved_selfie_provider: str,
        selfie_composite_mode: str,
    ) -> None:
        """Process every fan-out branch (expand -> video -> rPPG -> loop ->
        oldcam) after the primary chain completed. Branch results land in the
        video_generate step's meta["branches"]; a branch failure is reported
        but NEVER fails the case (the primary deliverable already exists)."""
        if not extra_branches:
            return
        results: List[Dict[str, Any]] = []
        try:
            for index, branch in enumerate(extra_branches):
                endpoint = branch["endpoint"]
                if self.abort_event.is_set():
                    # [a] must stop branch fan-out too — and it must RAISE,
                    # not continue: returning normally would let the caller
                    # finalize the case "completed", which resume then
                    # SKIPS — the selected branches would never run
                    # (Codex P1, round 3). Record every remaining branch
                    # first (the finally persists), then propagate so the
                    # case reverts to pending for resume.
                    remaining = [b["endpoint"] for b in extra_branches[index:]]
                    self._report(
                        f"[{case_key}] abort requested — remaining fan-out branch(es) "
                        f"recorded as aborted: {', '.join(remaining)}.",
                        "warning",
                    )
                    for rem in remaining:
                        results.append({"endpoint": rem, "status": "skipped", "error": "aborted"})
                    raise AutomationAbort("aborted between fan-out branches")
                self._report(
                    f"[{case_key}] fan-out branch: {endpoint} (score {branch['score']})",
                    "info",
                )
                try:
                    results.append(
                        self._run_branch_chain(
                            case_key=case_key,
                            case_dir=case_dir,
                            branch=branch,
                            reprocess_mode=reprocess_mode,
                            resolved_selfie_provider=resolved_selfie_provider,
                            selfie_composite_mode=selfie_composite_mode,
                        )
                    )
                except AutomationAbort as abort_exc:
                    # Never swallow an abort into a "branch failed" record —
                    # it must propagate to run()'s case-level abort handler
                    # (code-review MEDIUM, PR #96). Persist the PARTIAL
                    # result carried on the exception (fields recorded
                    # before the abort, e.g. a landed video) so resume can
                    # reuse the paid work; the finally below writes it out.
                    partial = getattr(abort_exc, "partial_result", None)
                    results.append(
                        partial or {"endpoint": endpoint, "status": "skipped", "error": "aborted"}
                    )
                    raise
                except Exception as exc:  # never let a branch kill the case
                    self.logger.exception("case %s branch %s crashed", case_key, endpoint)
                    self._report(f"[{case_key}] branch {endpoint} failed: {exc}", "error")
                    results.append({"endpoint": endpoint, "status": "failed", "error": str(exc)})
        finally:
            # Persist the branch summary even when an abort propagates —
            # losing the records of branches that DID finish would cost
            # redundant paid regeneration on resume (Gemini HIGH, PR #96).
            # Stored on the video_generate step so the manifest carries the
            # full fan-out picture without changing the one-status-per-step
            # schema. Guarded: an exception INSIDE a finally would REPLACE
            # the propagating AutomationAbort and break the abort contract
            # (round-3 review — update_step validates its status arg), so
            # a persistence failure is logged, never raised from here.
            try:
                video_step = self.manifest.get_step(case_key, "video_generate")
                merged_meta = dict(video_step.get("meta") or {})
                merged_meta["branches"] = results
                self.manifest.update_step(
                    case_key,
                    "video_generate",
                    video_step.get("status") or "complete",
                    output=video_step.get("output"),
                    meta=merged_meta,
                )
            except Exception:
                self.logger.exception(
                    "case %s: failed to persist branch summary (non-fatal)", case_key
                )

    def _run_branch_chain(
        self,
        *,
        case_key: str,
        case_dir: Path,
        branch: Dict[str, Any],
        reprocess_mode: str,
        resolved_selfie_provider: str,
        selfie_composite_mode: str,
    ) -> Dict[str, Any]:
        """One fan-out branch: expand the branch selfie, generate its video,
        then apply the same post chain (rPPG -> Loop -> Oldcam). Reuses
        existing outputs on resume (skip semantics: deterministic expand
        name; newest video derived from the branch's expanded stem)."""
        endpoint = branch["endpoint"]
        still_path = str(branch["path"])
        result: Dict[str, Any] = {
            "endpoint": endpoint,
            "selfie": still_path,
            "score": branch["score"],
            "status": "failed",
        }

        def _abort_checkpoint(stage: str) -> None:
            # Same contract as the primary chain's _set_active_step: [a]
            # stops after the CURRENT stage, never mid-stage (Gemini MED,
            # PR #96 — without these a branch ran every remaining paid
            # stage after the abort). The PARTIAL result rides on the
            # exception so the caller persists fields recorded before the
            # abort (e.g. an already-landed branch video) instead of a
            # bare skipped record — losing them cost a paid regeneration
            # on resume (round-3 review must-fix).
            if self.abort_event.is_set():
                result["status"] = "skipped"
                result["error"] = f"aborted before branch stage {stage}"
                exc = AutomationAbort(f"aborted before branch stage {stage} ({endpoint})")
                exc.partial_result = result
                raise exc

        # --- expand (same geometry/prompt rules as primary Step 5) ---
        final_still = still_path
        if self.automation.get("automation_selfie_expand_enabled", True):
            expanded_output = case_dir / "gen-images" / f"{Path(still_path).stem}-expanded.png"
            if reprocess_mode == "skip" and expanded_output.exists():
                final_still = str(expanded_output)
            else:
                _abort_checkpoint("selfie_expand")
                if reprocess_mode == "increment":
                    expanded_output = self._next_increment_path(expanded_output)
                pct = self._read_int("automation_selfie_expand_percent", 30)
                with Image.open(still_path) as _img:
                    width, height = ImageOps.exif_transpose(_img).size
                plan = compute_percent_expand_plan(
                    width, height, pct, compute_provider_caps(resolved_selfie_provider),
                )
                _section = self.config.get("selfie_expand_prompt")
                if isinstance(_section, str):
                    expand_prompt = _section
                else:
                    expand_prompt = str(self.config.get("outpaint_prompt", "") or "")
                outpaint = self.deps.outpaint_factory()
                outpaint.set_progress_callback(self.progress_cb)
                expanded_result = outpaint.outpaint(
                    image_path=still_path,
                    output_folder=str(case_dir / "gen-images"),
                    output_path=str(expanded_output),
                    provider=resolved_selfie_provider,
                    composite_mode=selfie_composite_mode,
                    document_mode=self.automation.get("automation_selfie_expand_mode") == "centered_3x4",
                    expand_left=int(plan["left"]),
                    expand_right=int(plan["right"]),
                    expand_top=int(plan["top"]),
                    expand_bottom=int(plan["bottom"]),
                    edge_seal_px=0,
                    poll_timeout_seconds=get_outpaint_fal_timeout_seconds(self.config),
                    prompt=expand_prompt,
                )
                if not expanded_result:
                    result["error"] = _with_outpaint_detail(
                        "branch selfie expand failed", outpaint
                    )
                    return result
                final_still = expanded_result
        result["expanded"] = final_still

        # --- video ---
        branch_video: Optional[str] = None
        if self.automation.get("automation_video_enabled", True):
            video_output_dir = case_dir / "gen-videos"
            video_output_dir.mkdir(exist_ok=True)
            expanded_stem = Path(final_still).stem
            if reprocess_mode == "skip" and self.automation.get("automation_skip_if_video_exists", True):
                reusable = sorted(
                    p for p in video_output_dir.glob(f"{expanded_stem}*.mp4")
                    if not is_rppg_artifact(p)
                    and "-oldcam-" not in p.stem
                    and "_looped" not in p.stem
                )
                if reusable:
                    branch_video = str(reusable[-1])
            if branch_video is None:
                _abort_checkpoint("video_generate")
                video = self.deps.video_factory()
                video.set_progress_callback(self.progress_cb)
                branch_video = self._generate_video_for_still(video, final_still, video_output_dir)
            if not branch_video:
                result["error"] = "branch video generation failed"
                return result
            if reprocess_mode == "increment":
                out_video_path = Path(branch_video)
                inc_video_path = self._next_increment_path(out_video_path)
                if inc_video_path != out_video_path:
                    out_video_path.replace(inc_video_path)
                    branch_video = str(inc_video_path)
            result["video"] = branch_video
        else:
            result["status"] = "complete"
            return result

        # --- post chain: rPPG -> Loop -> Oldcam (graceful-skip parity).
        # A branch failure never fails the CASE (the primary deliverable
        # exists), but the branch RECORD must be honest: a missing REQUIRED
        # deliverable marks the branch "failed", not "complete" (Codex P2,
        # PR #96 — the old code reported empty oldcam fan-outs as success).
        branch_issues: List[str] = []
        current = Path(branch_video)
        if self._read_bool("automation_rppg_enabled", False) and not is_rppg_artifact(current):
            # Parity with the primary pre-pass (Gemini HIGH, round 4): a
            # resumed branch must reuse its clean-named -rppg sibling
            # instead of re-running the minutes-long GPU injection.
            existing_branch_rppg = current.with_name(f"{current.stem}-rppg{current.suffix}")
            if reprocess_mode == "skip" and existing_branch_rppg.exists():
                current = existing_branch_rppg
                result["rppg"] = str(existing_branch_rppg)
                self._report(
                    f"[{case_key}] branch {endpoint}: reusing existing rPPG base "
                    f"{existing_branch_rppg.name}",
                    "info",
                )
            else:
                _abort_checkpoint("rppg")
                injected = self._branch_run_rppg(current)
                if injected and injected.exists():
                    current = injected
                    result["rppg"] = str(injected)
                elif self._read_bool("automation_rppg_required", False):
                    branch_issues.append("required rPPG produced no output")
        if self._read_bool("automation_loop_enabled", False):
            _abort_checkpoint("loop")
            looped = create_looped_video(
                str(current),
                suffix="_looped",
                # Same skip-mode reuse as the primary loop step (Gemini MED).
                overwrite=(reprocess_mode != "skip"),
                log_callback=self.progress_cb,
            )
            if looped and Path(looped).exists():
                current = Path(looped)
                result["loop"] = str(looped)
        if self._oldcam_active():
            _abort_checkpoint("oldcam")
            oldcam_all = run_oldcam_all(
                video_path=current,
                version_setting=self.automation.get("automation_oldcam_version", []),
                repo_root=Path(__file__).resolve().parent.parent,
                progress_cb=self.progress_cb,
            )
            result["oldcam_outputs"] = [str(p) for _v, p in oldcam_all]
            # _read_bool (not raw bool()) — bool("false") is True (round-2
            # review LOW; matches the rppg_required check above).
            if not oldcam_all and self._read_bool("automation_oldcam_required", False):
                branch_issues.append("required oldcam produced no outputs")
        if branch_issues:
            result["status"] = "failed"
            result["error"] = "; ".join(branch_issues)
            self._report(
                f"[{case_key}] branch {endpoint}: {result['error']}", "warning",
            )
        else:
            result["status"] = "complete"
        return result

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

    def _effective_reprocess_mode(self, case_key: Optional[str] = None) -> str:
        if case_key and case_key in self._force_regen_cases:
            # Front image changed for this case: regenerate every step
            # non-destructively (old outputs keep their names, new ones get
            # incremented names) regardless of the global skip/reuse policy.
            return "increment"
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

    @staticmethod
    def _front_expand_stage1_path(output_path: Path) -> Path:
        """The pass-1 intermediate sibling of a front-expand output:
        ``<stem>-stage1<ext>`` (mirrors the producer at the 2-pass loop)."""
        return output_path.with_name(
            f"{output_path.stem}-stage1{output_path.suffix or '.png'}"
        )

    def _front_expand_reuse_satisfies_passes(self, output_path: Path, passes: int) -> bool:
        """Whether reusing ``output_path`` actually satisfies the configured
        pass count. A 2-pass front expand is only honored on disk when its
        stage-1 sibling is ALSO present — a single-pass or pre-stage-1-retention
        (pre-#98) output lacks it, so the configured 2-pass must actually RUN to
        produce the larger fully-expanded result the user asked for (run-2x
        reuse bug, 2026-06-14). Single-pass configs are always satisfiable."""
        if passes != 2:
            return True
        return self._front_expand_stage1_path(output_path).exists()

    def _front_expand_reuse_meta(
        self, output_path: Path, passes: int, composite_mode: str, mode: str
    ) -> Dict[str, Any]:
        """Manifest meta for a SKIP-reused front-expand output. Records the
        stage-1 sibling path for parity with the freshly-executed path when a
        valid 2-pass output is reused."""
        meta: Dict[str, Any] = {
            **self._policy_meta("front_expand", True, mode),
            "configured_passes": passes,
            "executed_passes": 0,
            "composite_mode": composite_mode,
        }
        if passes == 2:
            stage1 = self._front_expand_stage1_path(output_path)
            if stage1.exists():
                meta["stage1_output"] = str(stage1)
        return meta

    def _set_active_step(self, case_entry: Dict[str, Any], step_name: Optional[str]) -> None:
        # Abort checkpoint: every step transition is a safe stopping point
        # (no half-written outputs — each step finishes or is re-runnable).
        if step_name is not None and self.abort_event.is_set():
            raise AutomationAbort(f"aborted before step {step_name}")
        # Mutation under the manifest lock: the dashboard's snapshot reader
        # holds it while reading these exact fields (code-review HIGH, PR #96).
        with self.manifest.lock:
            case_entry["active_step"] = step_name
            self.manifest.save_atomic()

    def _resolve_outpaint_provider(self, configured_provider: str) -> str:
        normalized = str(configured_provider or "auto").strip().lower()
        if normalized in {"bfl", "fal"}:
            return normalized
        if self.config.get("bfl_api_key"):
            return "bfl"
        return "fal"

    def _resolve_composite_mode(self, stage: str) -> str:
        stage_key = f"automation_{stage}_expand_composite_mode"
        configured_mode = str(
            self.automation.get(
                stage_key,
                self.config.get("outpaint_composite_mode", self._DEFAULT_OUTPAINT_COMPOSITE_MODE),
            )
        ).strip().lower()
        if configured_mode in self._VALID_OUTPAINT_COMPOSITE_MODES:
            return configured_mode
        return self._DEFAULT_OUTPAINT_COMPOSITE_MODE

    def resolve_provider_summary(self) -> Dict[str, str]:
        # Fallback default flipped to "fal" 2026-05-22: a live config
        # without these keys still gets the v2.3 ship default
        # (user direction final). The DEFAULTS dict in
        # automation/config.py was updated to match; this fallback
        # only kicks in when neither the live config nor the merged
        # defaults have the key set (rare path, but consistent).
        front_configured = str(self.automation.get("automation_front_expand_provider", "fal")).lower()
        selfie_configured = str(self.automation.get("automation_selfie_expand_provider", "fal")).lower()
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

    def _apply_one_postproc_step(self, step, current: Path, repo_root: Path,
                                 overwrite: bool) -> Optional[Path]:
        """Apply ONE post-processing step to ``current`` and return its output.

        Reuses the exact per-step processors the cascade calls. Returns the
        produced Path, the unchanged input when the step is a no-op (rPPG on an
        already-injected file), or ``None`` on a graceful skip (tool missing /
        encode failure) so the caller abandons that variant. In skip mode
        (``overwrite=False``) deterministic siblings are reused instead of
        re-encoded — this is what gives the powerset prefix-sharing for free.
        """
        from automation.postproc_plan import Modifier

        m = step.modifier
        if m is Modifier.RPPG:
            if is_rppg_artifact(current):
                return current
            sibling = current.with_name(f"{current.stem}-rppg{current.suffix}")
            if not overwrite and sibling.exists():
                return sibling
            rppg_mode = str(self.automation.get("automation_rppg_mode") or "iterative").strip().lower()
            out = run_rppg(
                video_path=current,
                repo_root=repo_root,
                progress_cb=self.progress_cb,
                keep_metrics=self._read_bool("automation_rppg_metrics_in_filename", False),
                iterative=(rppg_mode == "iterative"),
                iterate_from_baseline=self._read_bool("automation_rppg_iterate_from_baseline", True),
                skip_diagnosis=self._read_bool("automation_rppg_skip_diagnosis", True),
                skip_kinematic_gate=self._read_bool("automation_rppg_skip_kinematic_gate", True),
                landmark_stride=self._read_int("automation_rppg_landmark_stride", 1, min_value=1),
                verbose=self.verbose_logging,
            )
            return Path(out) if (out and Path(out).exists()) else None
        if m is Modifier.LOOP:
            looped = create_looped_video(
                str(current), suffix="_looped", overwrite=overwrite,
                log_callback=self.progress_cb,
            )
            return Path(looped) if (looped and Path(looped).exists()) else None
        if m is Modifier.CRUSH:
            height = CRUSH_RESOLUTIONS.get(step.option or "")
            if not height:
                return None
            crushed = crush_video(
                str(current), suffix=crush_suffix(step.option or ""),
                target_height=height, overwrite=overwrite,
                log_callback=self.progress_cb,
            )
            return Path(crushed) if (crushed and Path(crushed).exists()) else None
        if m is Modifier.AA:
            # Skip-mode reuse, like the other steps (CodeRabbit Major): the AA
            # output stem is deterministic, so in skip mode reuse an existing
            # sibling instead of re-running the (slow, isolated-venv) attack.
            expected = current.with_name(
                f"{current.stem}{aa_suffix(step.option or '')}{current.suffix}"
            )
            if not overwrite and expected.exists():
                return expected
            generator = str(self.automation.get("automation_aa_generator", "generic") or "")
            produced = run_aa(
                str(current), attack=step.option,
                strength=self._read_float("automation_aa_strength", 0.5),
                generator=generator or None,
                log_callback=self.progress_cb, repo_root=repo_root,
            )
            return Path(produced) if (produced and Path(produced).exists()) else None
        if m is Modifier.OLDCAM:
            expected = build_oldcam_output_path(current, step.option or "")
            if not overwrite and expected.exists():
                return expected
            out = run_oldcam_version(
                video_path=current, version=step.option or "",
                repo_root=repo_root, progress_cb=self.progress_cb,
            )
            return Path(out) if (out and Path(out).exists()) else None
        return None

    def _run_powerset_extras(self, *, case_key: str,
                             rppg_base_path: Optional[Path],
                             reprocess_mode: str) -> List[Path]:
        """Produce the proper-subset (powerset) post-processing variants.

        Only runs in ``separate_and_combined`` fan-out mode. The existing
        cascade already produced the FULL combined chain (every enabled
        modifier) + its sub-fan-out; this adds every PROPER subset of the
        enabled modifier types so the user also gets each modifier — and each
        intermediate combo — standalone, every variant sequenced in the
        canonical order from the raw Kling base.

        Variants are written to disk + logged but NOT manifest-tracked as
        deliverables: the cascade's full-chain output remains the primary
        deliverable, so the resume / finished_at contract is untouched. On
        resume the variants are re-derived cheaply (skip-mode on-disk reuse).
        Graceful-skip per step; aborts promptly between variants.
        """
        from automation.postproc_plan import build_plan, normalize_mode

        mode = normalize_mode(self.automation.get("automation_postproc_fanout_mode"))
        if mode != "separate_and_combined":
            return []
        # Abort already pending → add nothing and let the existing post-chain
        # abort checkpoints (_run_extra_branches) handle the revert. Powerset
        # extras are BONUS outputs; they must never raise their own abort that
        # would bypass branch-metadata registration (the finally in
        # _run_extra_branches), which resume depends on.
        if self.abort_event.is_set():
            return []
        # The raw Kling output is every variant's starting point. rPPG-prefixed
        # recipes inject on these clean frames (the load-bearing constraint).
        video_step = self.manifest.get_step(case_key, "video_generate")
        video_out = video_step.get("output") if isinstance(video_step, dict) else None
        raw_base = Path(video_out) if video_out else None
        if raw_base is None or not raw_base.exists() or raw_base.suffix.lower() != ".mp4":
            return []
        # On a resume path the reused video_generate output can ITSELF already be
        # an rPPG artifact (pre-injected upstream). Powerset's non-rPPG subsets
        # must derive from clean frames, so skip the fan-out rather than build
        # variants off an rPPG'd base (code-reviewer LOW; rare edge).
        if is_rppg_artifact(raw_base):
            self.logger.info(
                "case %s powerset skipped: base %s is already an rPPG artifact",
                case_key, raw_base.name,
            )
            return []

        repo_root = Path(__file__).resolve().parent.parent
        oldcam_versions = self._oldcam_versions() if self._oldcam_active() else []
        if oldcam_versions == ["all"]:
            # Expand the symbolic "all" so each version is a concrete variant.
            oldcam_versions = discover_oldcam_versions(repo_root)

        plan = build_plan(
            rppg_enabled=self._read_bool("automation_rppg_enabled", False),
            loop_enabled=self._read_bool("automation_loop_enabled", False),
            crush_resolutions=self._crush_resolutions(),
            aa_attacks=self._aa_attacks(),
            oldcam_versions=oldcam_versions,
            mode="separate_and_combined",
        )
        enabled = set(plan.enabled_modifiers)
        if len(enabled) < 2:
            # Powerset == the single full chain; the cascade already produced it.
            return []

        overwrite = reprocess_mode != "skip"
        # Prefix-memo cache (key = tuple of (modifier, option) for the applied
        # prefix). Seed the rPPG-on-raw prefix with the cascade's already-
        # injected base so we never re-run the expensive GPU injection.
        cache: Dict[tuple, Path] = {}
        if rppg_base_path is not None and Path(rppg_base_path).exists():
            cache[(("rppg", None),)] = Path(rppg_base_path)

        produced: List[Path] = []
        raw_base = Path(raw_base)
        # Surface the fan-out size up front — powerset is bounded but can be
        # large (2^N-1 x sub-options); the count is the user's cost guardrail
        # (code-reviewer LOW; no silent caps).
        extra_count = sum(1 for r in plan.recipes if set(r.modifiers()) != enabled)
        self.logger.info(
            "case %s powerset fan-out: producing %d extra variant(s)",
            case_key, extra_count,
        )
        for recipe in plan.recipes:
            if set(recipe.modifiers()) == enabled:
                continue  # the full chain — cascade already produced it.
            if self.abort_event.is_set():
                # Stop producing bonus variants promptly; do NOT raise (see the
                # entry guard above) — the downstream checkpoint reverts the case.
                break
            current = raw_base
            prefix: tuple = ()
            ok = True
            for step in recipe.steps:
                if self.abort_event.is_set():
                    ok = False
                    break
                prefix = prefix + ((step.modifier.value, step.option),)
                if prefix in cache:
                    current = cache[prefix]
                    continue
                nxt = self._apply_one_postproc_step(step, current, repo_root, overwrite)
                if nxt is None:
                    ok = False
                    break
                cache[prefix] = nxt
                current = nxt
            if ok and current != raw_base and current.exists():
                produced.append(current)

        # Mirror of the GUI fan-out cleanup (kling_gui/queue_manager.py): reclaim
        # memory after the fan-out and prune STRICT intermediates (cached step
        # outputs no recipe claimed as a deliverable). No-op in full powerset
        # mode; preserves deliverables, the raw base, and the rPPG seed. Opt out
        # with KLING_KEEP_POSTPROC_INTERMEDIATES=1.
        if os.environ.get("KLING_KEEP_POSTPROC_INTERMEDIATES") != "1":
            from automation.postproc_cleanup import prune_strict_intermediates
            keep = set(produced) | {raw_base}
            if rppg_base_path is not None:
                keep.add(Path(rppg_base_path))
            prune_strict_intermediates(
                cache.values(), keep,
                on_pruned=lambda name: self.logger.debug(
                    "pruned post-proc intermediate: %s", name
                ),
            )
        gc.collect()

        if produced:
            self.logger.info(
                "case %s powerset extras: %d variant(s): %s",
                case_key, len(produced), ", ".join(p.name for p in produced),
            )
            self._report(
                f"[{case_key}] Powerset fan-out: {len(produced)} extra variant(s) produced",
                "info",
            )
        return produced

    def _finalize_case(self, case_entry: Dict[str, Any], final_status: str) -> str:
        status_value = "complete" if final_status == "completed" else final_status
        with self.manifest.lock:
            case_entry["status"] = status_value
            # _set_active_step commits via save_atomic — no second save here
            # (round-2 review MED: the lock wrapper had doubled the disk
            # write per finalization).
            self._set_active_step(case_entry, None)
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

        similarity_threshold = self._read_int("automation_similarity_threshold", 80, issues, min_value=0, max_value=100)
        front_mode = str(self.automation.get("automation_front_expand_mode", "percent")).lower()
        front_composite_mode = self._resolve_composite_mode("front")
        selfie_composite_mode = self._resolve_composite_mode("selfie")
        raw_front_composite_mode = str(
            self.automation.get(
                "automation_front_expand_composite_mode",
                self.config.get("outpaint_composite_mode", self._DEFAULT_OUTPAINT_COMPOSITE_MODE),
            )
        ).strip().lower()
        raw_selfie_composite_mode = str(
            self.automation.get(
                "automation_selfie_expand_composite_mode",
                self.config.get("outpaint_composite_mode", self._DEFAULT_OUTPAINT_COMPOSITE_MODE),
            )
        ).strip().lower()
        if raw_front_composite_mode not in self._VALID_OUTPAINT_COMPOSITE_MODES:
            issues.append(
                "automation_front_expand_composite_mode must be one of: preserve_seamless, feathered, hard, none, black_fill."
            )
        if raw_selfie_composite_mode not in self._VALID_OUTPAINT_COMPOSITE_MODES:
            issues.append(
                "automation_selfie_expand_composite_mode must be one of: preserve_seamless, feathered, hard, none, black_fill."
            )
        front_expand_percent = (
            self._read_int("automation_front_expand_percent", 30, issues, min_value=0)
            if front_mode == "percent"
            else 30
        )
        selfie_expand_percent = self._read_int("automation_selfie_expand_percent", 30, issues, min_value=0)
        crop_multiplier = self._read_float("automation_crop_multiplier", 1.5, issues, min_value=0.01)
        selfie_attempts = self._read_int("automation_selfie_max_attempts_per_model", 1, issues, min_value=1)
        front_passes = self._read_int("automation_front_expand_passes", 2, issues)
        if front_passes not in {1, 2}:
            issues.append("automation_front_expand_passes must be 1 or 2.")
        # Keep variables consumed so validation checks stay explicit and deterministic.
        _ = (
            similarity_threshold,
            front_expand_percent,
            selfie_expand_percent,
            crop_multiplier,
            selfie_attempts,
            front_composite_mode,
            selfie_composite_mode,
        )
        if self.automation.get("automation_oldcam_required", False) and not self.automation.get("automation_oldcam_enabled", True):
            issues.append("automation_oldcam_required=true requires automation_oldcam_enabled=true.")
        # Symmetric with the oldcam rule above (Codex P2, PR #39): the CLI
        # advanced setup asks rppg enabled/required independently, so a
        # user can save automation_rppg_required=true while
        # automation_rppg_enabled=false. Without this, Step 8 records the
        # rPPG step "skipped" and the case still finalizes complete — the
        # "required" policy silently becomes a no-op. Reject the
        # contradictory combination at config-validation time, exactly as
        # oldcam does.
        if self._read_bool("automation_rppg_required", False) and not self._read_bool("automation_rppg_enabled", False):
            issues.append("automation_rppg_required=true requires automation_rppg_enabled=true.")
        _crush_on = bool(self._crush_resolutions())
        if self._read_bool("automation_crush_required", False) and not _crush_on:
            issues.append("automation_crush_required=true requires at least one crush resolution selected.")
        if self._read_bool("automation_loop_enabled", False) or _crush_on:
            ffmpeg_ok, ffmpeg_msg = check_ffmpeg_available()
            if not ffmpeg_ok:
                if self._read_bool("automation_crush_required", False):
                    # Crush required but ffmpeg missing: hard-fail at validation
                    # so the operator learns before any paid API calls are made.
                    issues.append(
                        f"automation_crush_required=true but ffmpeg unavailable: {ffmpeg_msg}"
                    )
                else:
                    # Graceful-skip at runtime; surface as WARNING so the user
                    # knows before a paid run that these steps will be skipped.
                    self._report(
                        f"Loop/Crush enabled but ffmpeg unavailable — those steps will be skipped ({ffmpeg_msg}).",
                        "warning",
                    )
        # AA (adversarial-attack) preflight: needs both the aa-video launcher
        # AND ffmpeg. Mirrors the crush rule — hard-fail at validation when
        # required, else WARNING so the operator knows it'll skip before paying
        # for the run.
        _aa_on = bool(self._aa_attacks())
        if self._read_bool("automation_aa_required", False) and not _aa_on:
            issues.append("automation_aa_required=true requires at least one AA attack selected.")
        if _aa_on:
            aa_ok, aa_msg = check_aa_available(Path(__file__).resolve().parent.parent)
            if not aa_ok:
                if self._read_bool("automation_aa_required", False):
                    issues.append(f"automation_aa_required=true but AA unavailable: {aa_msg}")
                else:
                    self._report(
                        f"AA enabled but unavailable — that step will be skipped ({aa_msg}).",
                        "warning",
                    )
        if self.automation.get("automation_oldcam_required", False):
            repo_root = Path(__file__).resolve().parent.parent
            versions = discover_oldcam_versions(repo_root)
            deps_ok, deps_error = ensure_oldcam_dependencies()
            configured_versions = normalize_oldcam_versions(
                self.automation.get("automation_oldcam_version", ["all"])
            )
            available_versions = {str(v).lower() for v in versions}
            if not configured_versions:
                issues.append(
                    "automation_oldcam_required=true but no oldcam versions are selected "
                    "(automation_oldcam_version is empty)."
                )
            elif configured_versions == ["all"]:
                if not available_versions:
                    issues.append("Oldcam required with version=all but no usable oldcam versions were discovered.")
            else:
                missing = [v for v in configured_versions if v not in available_versions]
                if missing:
                    issues.append(
                        f"Oldcam required but configured version(s) {', '.join(missing)} unavailable. "
                        f"Available: {', '.join(sorted(available_versions)) or '(none)'}."
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
            resolve_cli_video_model(self.config)[1] or resolve_cli_video_model(self.config)[0],
            resolve_cli_kling_prompt_slot(self.config),
        )
        validation_issues = self.validate_configuration()
        if validation_issues:
            self.logger.error("automation validation failed: %s", validation_issues)
            raise ValueError("Configuration validation failed: " + "; ".join(validation_issues))

        stats = {"completed": 0, "failed": 0, "manual_review": 0, "skipped": 0}
        for case in cases:
            if self.abort_event.is_set() or self.pause_event.is_set():
                self.stopped_reason = "aborted" if self.abort_event.is_set() else "paused"
                self._report(
                    f"Run {self.stopped_reason} — remaining cases stay pending; "
                    "use Run/Resume to continue.",
                    "warning",
                )
                self.logger.info("automation run %s before case %s", self.stopped_reason, case.relative_key)
                break
            self.logger.info("case start: %s", case.relative_key)
            self._reset_case_if_front_changed(case)
            self.manifest.ensure_case(case.relative_key, case.case_dir, case.front_path)
            if self.automation.get("automation_skip_completed", True) and self.manifest.case_is_complete_and_valid(case.relative_key):
                stats["skipped"] += 1
                self.last_case_results[case.relative_key] = {"status": "skipped", "reason": "already complete"}
                self.logger.info("case skipped complete: %s", case.relative_key)
                continue
            try:
                final_status = self._run_case(case)
            except AutomationAbort:
                # Revert the in-flight case to "pending": every completed
                # step's manifest state is kept, so Run/Resume continues it
                # from the first incomplete step.
                self.stopped_reason = "aborted"
                with self.manifest.lock:
                    cases_map = self.manifest.data.setdefault("cases", {})
                    case_state = cases_map.setdefault(case.relative_key, {})
                    case_state["status"] = "pending"
                    case_state["active_step"] = None
                    case_state["updated_at"] = now_iso()
                    self.manifest.save_atomic()
                self._report(
                    f"[{case.relative_key}] aborted mid-case — progress saved; "
                    "Run/Resume continues from the next incomplete step.",
                    "warning",
                )
                self.logger.info("case aborted (reverted to pending): %s", case.relative_key)
                break
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
        manifest_steps = case_entry.get("steps", {}) if isinstance(case_entry.get("steps"), dict) else {}
        manifest_selfie_output = manifest_steps.get("selfie_generate", {}).get("output")
        if manifest_selfie_output:
            manifest_selfie_path = Path(manifest_selfie_output)
            if manifest_selfie_path.exists() and manifest_selfie_path.is_file():
                existing = existing.__class__(
                    front_expanded=existing.front_expanded,
                    extracted=existing.extracted,
                    selfie_candidate=manifest_selfie_path,
                    video_candidate=existing.video_candidate,
                )
        manifest_video_output = manifest_steps.get("video_generate", {}).get("output")
        if manifest_video_output:
            manifest_video_path = Path(manifest_video_output)
            if manifest_video_path.exists() and manifest_video_path.is_file() and manifest_video_path.suffix.lower() == ".mp4":
                existing = existing.__class__(
                    front_expanded=existing.front_expanded,
                    extracted=existing.extracted,
                    selfie_candidate=existing.selfie_candidate,
                    video_candidate=manifest_video_path,
                )
        with self.manifest.lock:
            case_entry["status"] = "running"
            # _set_active_step commits via save_atomic (round-2 review MED).
            self._set_active_step(case_entry, None)

        outpaint = self.deps.outpaint_factory()
        outpaint.set_progress_callback(self.progress_cb)
        reprocess_mode = self._effective_reprocess_mode(case_key)
        front_provider = str(self.automation.get("automation_front_expand_provider", "fal")).lower()
        selfie_provider = str(self.automation.get("automation_selfie_expand_provider", "fal")).lower()
        front_composite_mode = self._resolve_composite_mode("front")
        selfie_composite_mode = self._resolve_composite_mode("selfie")
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
        configured_front_passes = self._read_int("automation_front_expand_passes", 2)
        front_passes = configured_front_passes if configured_front_passes in {1, 2} else 2
        if self.automation.get("automation_front_expand_enabled", True):
            # black_fill is a deterministic local paste onto a black canvas; a
            # 2nd pass would re-border the already-bordered image, so it is
            # ALWAYS single-pass. Normalize the effective pass count BEFORE the
            # skip-reuse check below so the 2-pass stage-1 reuse guard doesn't
            # demand a stage-1 sibling that black_fill never produces (which
            # would re-run black_fill on every skip-run). Kept INSIDE the
            # enabled gate so a disabled front expand doesn't log spuriously.
            # The 2-pass design only benefits AI outpaint.
            if front_composite_mode == "black_fill" and front_passes != 1:
                self.logger.info(
                    "case %s front expand: black_fill forces single pass (was %s)",
                    case_key, front_passes,
                )
                front_passes = 1
            current_step = self.manifest.get_step(case_key, "front_expand")
            existing_front_step_output = current_step.get("output")
            if (
                reprocess_mode == "skip"
                and current_step.get("status") == "complete"
                and existing_front_step_output
                and Path(existing_front_step_output).exists()
                and self._front_expand_reuse_satisfies_passes(
                    Path(existing_front_step_output), front_passes
                )
            ):
                front_expanded = Path(existing_front_step_output)
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta=self._front_expand_reuse_meta(
                        front_expanded, front_passes, front_composite_mode, reprocess_mode
                    ),
                )
            elif (
                reprocess_mode == "skip"
                and front_expanded
                and Path(front_expanded).exists()
                # A prior attempt that was INTERRUPTED (status "running", e.g. a
                # crash/kill mid-2-pass) or FAILED must not be reused off disk:
                # a crash bypasses the orphaned-stage1 cleanup below, so an old
                # stale final + orphan stage1 could otherwise fool the 2-pass
                # reuse guard. Re-run instead (CodeRabbit Major, PR #99). A
                # never-run case (status "pending") with files on disk is the
                # legitimate disk-trust fallback and still reuses.
                and current_step.get("status") not in {"running", "failed"}
                and self._front_expand_reuse_satisfies_passes(
                    Path(front_expanded), front_passes
                )
            ):
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(front_expanded),
                    meta=self._front_expand_reuse_meta(
                        Path(front_expanded), front_passes, front_composite_mode, reprocess_mode
                    ),
                )
            else:
                target_output = Path(front_expanded)
                if reprocess_mode == "increment":
                    target_output = self._next_increment_path(target_output)
                front_is_document = self.automation.get("automation_front_expand_mode") == "document_3x4"
                front_pct = self._read_int("automation_front_expand_percent", 30)
                # NOTE: black_fill's single-pass force is applied earlier (right
                # after front_passes is computed) so the skip-reuse guard sees
                # the effective pass count; by here front_passes is already 1
                # for black_fill.
                self._set_active_step(case_entry, "front_expand")
                self.manifest.update_step(case_key, "front_expand", "running")
                result = None
                front_input_path = str(case.front_path)
                executed_passes = 0
                # For 2-pass mode: pass 1 must NOT overwrite pass 2's
                # output path (the prior `output_path=None` auto-name
                # in pass 1 produced `<stem>-expanded.png`, exactly
                # what pass 2's forced `target_output` was; pass 2
                # silently clobbered pass 1's file). Plan a distinct
                # intermediate path for pass 1 so both stages are
                # retained on disk and the downstream contract
                # (target_output = `front-expanded.png`) holds.
                # PR #48 round 4 subagent M3.
                # Shared stage-1 naming with the skip-reuse guard
                # (_front_expand_reuse_satisfies_passes) so the producer and the
                # reuse-validity check can never drift.
                _pass1_intermediate: Optional[str] = None
                if front_passes == 2:
                    _pass1_intermediate = str(
                        self._front_expand_stage1_path(target_output)
                    )
                for pass_index in range(front_passes):
                    if pass_index == front_passes - 1:
                        pass_output = str(target_output)
                    elif _pass1_intermediate:
                        pass_output = _pass1_intermediate
                    else:
                        pass_output = None
                    # Recompute the expand plan from THIS pass's actual input
                    # dimensions. Pass 1 reads the original front; pass 2 reads
                    # pass 1's (already-expanded) output. The previous code
                    # computed the plan ONCE from the original dims and reused
                    # the same absolute pixel margins on pass 2's larger image —
                    # so pass 2 asked the provider to expand an already-portrait
                    # canvas by margins sized for the small original, which the
                    # model filled with black borders (the user-reported bug).
                    # document_mode lets the provider plan its own 3:4 geometry.
                    front_expand_kwargs: Dict[str, Any] = {}
                    if not front_is_document:
                        with Image.open(front_input_path) as _img:
                            _pw, _ph = ImageOps.exif_transpose(_img).size
                        _plan = compute_percent_expand_plan(
                            _pw,
                            _ph,
                            front_pct,
                            compute_provider_caps(resolved_front_provider),
                        )
                        front_expand_kwargs = {
                            "expand_left": int(_plan["left"]),
                            "expand_right": int(_plan["right"]),
                            "expand_top": int(_plan["top"]),
                            "expand_bottom": int(_plan["bottom"]),
                        }
                        self.logger.info(
                            "case %s front expand pass %d/%d geometry width=%s height=%s pct=%s plan=%s",
                            case_key, pass_index + 1, front_passes, _pw, _ph, front_pct, _plan,
                        )
                    # Phase G of polish/v2.3 (2026-05-22): Step 0
                    # face-crop expand uses its OWN prompt key. Falls
                    # back to the legacy shared ``outpaint_prompt`` so
                    # CLI runs on configs without the new key still
                    # work. Pre-Phase-G this dispatch passed no prompt
                    # at all (default empty string), so the section-
                    # specific key is a strict improvement.
                    # Codex P1 on 0967564 (2026-05-22): use key-
                    # presence semantics — a user who saves an
                    # intentionally EMPTY ``face_crop_expand_prompt``
                    # must NOT see the legacy shared
                    # ``outpaint_prompt`` re-substituted. ``or "" or``
                    # treated empty as missing, breaking section-
                    # specific prompt independence. The helper below
                    # only falls back when the key is absent or
                    # non-str; an empty string is a valid intentional
                    # value.
                    _section = self.config.get("face_crop_expand_prompt")
                    if isinstance(_section, str):
                        front_expand_prompt = _section
                    else:
                        front_expand_prompt = str(
                            self.config.get("outpaint_prompt", "") or ""
                        )
                    result = outpaint.outpaint(
                        image_path=front_input_path,
                        output_folder=str(case_dir),
                        output_path=pass_output,
                        provider=resolved_front_provider,
                        composite_mode=front_composite_mode,
                        document_mode=front_is_document,
                        edge_seal_px=int(self.automation.get("automation_front_edge_seal_px", 12))
                        if self.automation.get("automation_front_edge_seal_enabled", True)
                        else 0,
                        poll_timeout_seconds=get_outpaint_fal_timeout_seconds(self.config),
                        prompt=front_expand_prompt,
                        **front_expand_kwargs,
                    )
                    if not result:
                        # A failed 2-pass attempt must NOT leave an orphaned
                        # stage-1 sibling on disk: a later reprocess=skip run
                        # would otherwise see (old stale final + orphan stage1)
                        # and wrongly treat it as a satisfied 2-pass, reusing the
                        # under-expanded final instead of re-running (CodeRabbit
                        # Major, PR #99). Cleaning the orphan at the source is
                        # robust to cross-OS folder copies, unlike an mtime
                        # comparison (this project routinely moves case folders
                        # between machines, scrambling mtimes).
                        if _pass1_intermediate:
                            try:
                                Path(_pass1_intermediate).unlink(missing_ok=True)
                            except OSError:
                                pass
                        self.manifest.update_step(
                            case_key,
                            "front_expand",
                            "failed",
                            error=_with_outpaint_detail(
                                f"front expansion failed on pass {pass_index + 1}",
                                outpaint,
                            ),
                            meta={
                                "configured_passes": front_passes,
                                "executed_passes": executed_passes,
                            },
                        )
                        return self._finalize_case(case_entry, "failed")
                    front_input_path = result
                    executed_passes += 1
                # Keep the pass-1 intermediate alongside the final output so
                # both expansion stages are available for review (user
                # request: "run 2x mode should save the expanded both
                # steps"). It's named `<final-stem>-stage1<ext>` (e.g.
                # front-expanded-stage1.png) next to front-expanded.png.
                # Earlier code unlinked it to avoid session_manager's
                # outpaint-classifier surfacing it as a sibling carousel
                # generation; that tradeoff is now reversed per direct user
                # direction. record its path in the manifest meta so callers
                # can find it deterministically.
                _stage1_kept: Optional[str] = None
                if front_passes == 2 and _pass1_intermediate and Path(_pass1_intermediate).exists():
                    _stage1_kept = _pass1_intermediate
                _front_meta: Dict[str, Any] = {
                    **self._policy_meta("front_expand", False, reprocess_mode),
                    "configured_passes": front_passes,
                    "executed_passes": executed_passes,
                    "composite_mode": front_composite_mode,
                }
                if _stage1_kept:
                    _front_meta["stage1_output"] = _stage1_kept
                self.manifest.update_step(
                    case_key,
                    "front_expand",
                    "complete",
                    output=str(result),
                    meta=_front_meta,
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
            manifest_extract_path = Path(existing_extract_output) if existing_extract_output else None
            discovered_extract_path = existing.extracted if existing.extracted and existing.extracted.exists() else None
            default_extract_path = extracted_path if extracted_path.exists() else None
            resolved_extract = (
                (manifest_extract_path if manifest_extract_path and manifest_extract_path.exists() else None)
                or discovered_extract_path
                or default_extract_path
            )
            if resolved_extract is not None:
                extracted_path = resolved_extract
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
                crop_multiplier=self._read_float("automation_crop_multiplier", 1.5),
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
                meta={"threshold": self._read_int("automation_similarity_threshold", 80)},
            )
            return self._finalize_case(case_entry, "manual_review")

        selfie = self.deps.selfie_factory()
        selfie.set_progress_callback(self.progress_cb)
        selfie_folder = case_dir / "gen-images"
        selfie_folder.mkdir(exist_ok=True)
        model_endpoints = list(self.automation.get("automation_selfie_models", ["fal-ai/nano-banana-2/edit"]))
        selfie_prompt_ctx = self.resolve_selfie_prompt()
        threshold = self._read_int("automation_similarity_threshold", 80)
        require_fas_pass = self._read_bool("automation_similarity_require_fas_pass", False)
        max_attempts = max(1, self._read_int("automation_selfie_max_attempts_per_model", 1))
        self.logger.info(
            "case %s selfie config models=%s prompt_slot=%s prompt_source=%s threshold=%s",
            case_key,
            model_endpoints,
            selfie_prompt_ctx["slot"],
            selfie_prompt_ctx["source"],
            threshold,
        )

        # Multi-model fan-out (2026-06-11): with N>1 selected models, EVERY
        # model whose best candidate passes the similarity threshold becomes
        # a branch — its selfie gets its own expand -> video -> post chain.
        # The overall-best candidate stays the PRIMARY (owns the manifest
        # step statuses, byte-identical single-model behavior); the others
        # are processed by _run_extra_branches after the primary completes.
        fan_out = len(model_endpoints) > 1
        branch_best: Dict[str, Dict[str, Any]] = {}
        best_endpoint: Optional[str] = None

        best_path: Optional[str] = str(existing.selfie_candidate) if (
            reprocess_mode == "skip"
            and existing.selfie_candidate
            and self.automation.get("automation_skip_if_selfie_exists", True)
        ) else None
        best_score = -1
        best_similarity_meta: Dict[str, Any] = {
            "score": None,
            "threshold": threshold,
            "match": None,
            "error": None,
            "diagnostics": None,
        }
        self._set_active_step(case_entry, "selfie_generate")
        self.manifest.update_step(case_key, "selfie_generate", "running")
        if best_path:
            score_info = compute_face_similarity_details(str(extracted_path), best_path, report_cb=self.progress_cb)
            # `or 0`: an error path can return {"score": None}; int(None)
            # would crash the case (Gemini MED, PR #96).
            best_score = int(score_info.get("score") or 0)
            best_similarity_meta = {
                "score": score_info.get("score"),
                "threshold": threshold,
                "match": score_info.get("match"),
                "error": score_info.get("error"),
                "diagnostics": score_info.get("diagnostics"),
            }
            self._report(f"[{case_key}] Reused existing selfie: {Path(best_path).name}", "info")
        else:
            # 3:4 selfie dimensions (864x1152 default). Passing these keeps the
            # whole chain 3:4: SelfieGenerator snaps to its nearest supported
            # aspect label (3:4), the ratio-preserving percent expand maintains
            # it, and Kling (which follows the input image's ratio) then yields
            # a 3:4 video. Without them the generator defaults to 720x1280 (9:16).
            selfie_w = self._read_int("automation_selfie_width", 864)
            selfie_h = self._read_int("automation_selfie_height", 1152)
            # Unattended batch automation passes the poll-timeout CEILING
            # (180s; SelfieGenerator clamps) unless the user configured one:
            # the interactive 120s default exists to surface stuck queues
            # fast to a watching user, but in a batch run it silently killed
            # GPT Image 2 Edit (~137s on a SUCCESSFUL generation — the exact
            # model the fan-out duo ships with; E2E round 3, 2026-06-11).
            selfie_poll_timeout = SelfieGenerator.sanitize_poll_timeout_seconds(
                self.config.get("selfie_poll_timeout_seconds")
                or SelfieGenerator.MAX_POLL_TIMEOUT_SECONDS
            )
            for endpoint in model_endpoints:
                for _attempt in range(max_attempts):
                    generated = selfie.generate(
                        image_path=str(extracted_path),
                        prompt=selfie_prompt_ctx["prompt"],
                        output_folder=str(selfie_folder),
                        model_endpoint=endpoint,
                        width=selfie_w,
                        height=selfie_h,
                        poll_timeout_seconds=selfie_poll_timeout,
                    )
                    if not generated:
                        continue
                    score_info = compute_face_similarity_details(str(extracted_path), generated, report_cb=self.progress_cb)
                    # `or 0`: an error path can return {"score": None};
                    # int(None) would crash the case (Gemini MED, PR #96).
                    score = int(score_info.get("score") or 0)
                    similarity_meta = {
                        "score": score_info.get("score"),
                        "threshold": threshold,
                        "match": score_info.get("match"),
                        "error": score_info.get("error"),
                        "diagnostics": score_info.get("diagnostics"),
                    }
                    prior = branch_best.get(endpoint)
                    if prior is None or score > prior["score"]:
                        branch_best[endpoint] = {
                            "endpoint": endpoint,
                            "path": generated,
                            "score": score,
                            "similarity": similarity_meta,
                        }
                    if score > best_score:
                        best_score = score
                        best_path = generated
                        best_endpoint = endpoint
                        best_similarity_meta = similarity_meta
                    if self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass" and score >= threshold:
                        break
                # Fan-out mode generates for EVERY selected model — the
                # cross-model first_pass early-exit only applies to
                # single-model runs (where it is the historical behavior).
                if (
                    not fan_out
                    and self.automation.get("automation_selfie_model_policy", "first_pass") == "first_pass"
                    and best_score >= threshold
                ):
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
        diag = best_similarity_meta.get("diagnostics") if isinstance(best_similarity_meta, dict) else None
        if not isinstance(diag, dict):
            diag = {}
        self.logger.info(
            "case %s similarity summary score=%s mode=%s distance=%s fallback=%s per_model=%s",
            case_key,
            best_similarity_meta.get("score"),
            diag.get("mode"),
            diag.get("raw_cosine_distance"),
            diag.get("fallback_reason"),
            diag.get("per_model_distances"),
        )
        # Route through summarize_fas_pair so the gate decision uses the same
        # verdict the GUI/CLI shows the user. The strict-pass gate ONLY blocks
        # on verdict="fail" (both sides have ok FAS data + at least one spoof
        # flagged). Verdict "unavailable" (one side missing FAS) NEVER blocks —
        # there's nothing to enforce against.
        fas_pair = None
        ref_spoof = None
        tgt_spoof = None
        spoof_blocking = False
        try:
            from similarity_engine import FaceEngine as _FaceEngine
            fas_pair = _FaceEngine.summarize_fas_pair(diag if isinstance(diag, dict) else None)
        except Exception:
            fas_pair = None
        if isinstance(fas_pair, dict):
            verdict = fas_pair.get("verdict")
            ref_status = fas_pair.get("ref_status")
            tgt_status = fas_pair.get("target_status")
            # Pull raw spoof flags for log readability (when ok-status).
            fas = diag.get("anti_spoofing") if isinstance(diag, dict) else None
            if isinstance(fas, dict):
                ref_fas = fas.get("ref") if isinstance(fas.get("ref"), dict) else {}
                tgt_fas = fas.get("target") if isinstance(fas.get("target"), dict) else {}
                ref_spoof = (ref_fas or {}).get("spoof_detected")
                tgt_spoof = (tgt_fas or {}).get("spoof_detected")
            if verdict == "fail":
                if require_fas_pass:
                    self.logger.warning(
                        "case %s anti-spoofing FAILED ref=%s target=%s (require_fas_pass=true; routing to manual_review)",
                        case_key,
                        ref_spoof,
                        tgt_spoof,
                    )
                    spoof_blocking = True
                else:
                    self.logger.warning(
                        "case %s anti-spoofing advisory ref=%s target=%s (log-only; gate unchanged)",
                        case_key,
                        ref_spoof,
                        tgt_spoof,
                    )
            elif verdict == "unavailable":
                self.logger.info(
                    "case %s anti-spoofing not assessable (ref_status=%s target_status=%s) — gate unchanged",
                    case_key,
                    ref_status,
                    tgt_status,
                )
        if self.verbose_logging:
            self.logger.debug("case %s similarity diagnostics=%s", case_key, best_similarity_meta)
        if spoof_blocking:
            spoof_reason = f"anti-spoofing failed (ref_spoof={ref_spoof}, target_spoof={tgt_spoof})"
            self.manifest.update_step(
                case_key,
                "similarity_gate",
                "manual_review",
                output=best_path,
                error=spoof_reason,
                meta=best_similarity_meta,
            )
            self.last_case_results[case_key] = {"status": "manual_review", "reason": spoof_reason}
            return self._finalize_case(case_entry, "manual_review")
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

        # Multi-model fan-out: collect the EXTRA branches (non-primary
        # models whose best candidate passed the threshold). Processed by
        # _run_extra_branches at every completed-finalize site below; a
        # branch failure never fails the case (the primary already
        # succeeded) — it is recorded in the video_generate step's
        # meta["branches"] for visibility.
        extra_branches: List[Dict[str, Any]] = []
        if fan_out:
            for endpoint in model_endpoints:
                cand = branch_best.get(endpoint)
                if cand is None and best_path:
                    cand = self._existing_branch_candidate(case_dir, endpoint, threshold)
                if cand is None:
                    if endpoint != best_endpoint:
                        # LOUD: a selected model silently producing nothing
                        # (e.g. generation timeout) must be visible — E2E
                        # round 3 lost the whole GPT branch to a quiet poll
                        # timeout with no trace in the run output.
                        self._report(
                            f"[{case_key}] branch {endpoint}: no usable selfie this run "
                            "(generation failed/timed out and nothing reusable on disk) — branch SKIPPED.",
                            "warning",
                        )
                    continue
                if str(cand["path"]) == str(best_path) or endpoint == best_endpoint:
                    continue  # the primary chain already covers this one
                if cand["score"] < threshold:
                    self._report(
                        f"[{case_key}] branch {endpoint}: best score {cand['score']} "
                        f"below threshold {threshold} — branch skipped.",
                        "warning",
                    )
                    continue
                extra_branches.append(cand)
            if extra_branches:
                self.logger.info(
                    "case %s fan-out branches=%s",
                    case_key,
                    [b["endpoint"] for b in extra_branches],
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
                    meta={
                        **self._policy_meta("selfie_expand", True, reprocess_mode),
                        "composite_mode": selfie_composite_mode,
                    },
                )
            else:
                pct = self._read_int("automation_selfie_expand_percent", 30)
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
                # Phase G: Step 2.5 selfie expand uses its OWN prompt
                # key. Codex P1 on 0967564: key-presence semantics,
                # NOT truthiness — an explicitly-saved empty string
                # is a valid intentional value (user clearing the
                # prompt) and must NOT be silently replaced by the
                # legacy shared prompt.
                _section = self.config.get("selfie_expand_prompt")
                if isinstance(_section, str):
                    selfie_expand_prompt = _section
                else:
                    selfie_expand_prompt = str(
                        self.config.get("outpaint_prompt", "") or ""
                    )
                expanded_result = outpaint.outpaint(
                    image_path=best_path,
                    output_folder=str(case_dir / "gen-images"),
                    output_path=str(expanded_output),
                    provider=resolved_selfie_provider,
                    composite_mode=selfie_composite_mode,
                    document_mode=self.automation.get("automation_selfie_expand_mode") == "centered_3x4",
                    expand_left=margins["left"],
                    expand_right=margins["right"],
                    expand_top=margins["top"],
                    expand_bottom=margins["bottom"],
                    edge_seal_px=0,
                    poll_timeout_seconds=get_outpaint_fal_timeout_seconds(self.config),
                    prompt=selfie_expand_prompt,
                )
                if expanded_result:
                    final_still = expanded_result
                    self.manifest.update_step(
                        case_key,
                        "selfie_expand",
                        "complete",
                        output=expanded_result,
                        meta={
                            **self._policy_meta("selfie_expand", False, reprocess_mode),
                            "composite_mode": selfie_composite_mode,
                        },
                    )
                else:
                    self.manifest.update_step(
                        case_key, "selfie_expand", "failed",
                        error=_with_outpaint_detail("selfie expand failed", outpaint),
                    )
                    return self._finalize_case(case_entry, "failed")
        else:
            self.manifest.update_step(
                case_key,
                "selfie_expand",
                "skipped",
                output=best_path,
                meta={
                    **self._policy_meta("selfie_expand", False, reprocess_mode),
                    "composite_mode": selfie_composite_mode,
                },
            )

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
                if not self._oldcam_active():
                    self.manifest.update_step(case_key, "oldcam", "skipped", error=self._oldcam_inactive_reason())
                    # Pre-rPPG this short-circuited to completed (video
                    # reused + oldcam off = nothing left). rPPG or Loop can
                    # now be the ONLY enabled post-process on a reused
                    # video, so only finalize early when BOTH are disabled —
                    # otherwise fall through so the rPPG/Loop blocks pick up
                    # the reused video from the video_generate step output.
                    if not self._read_bool("automation_rppg_enabled", False) and not self._read_bool(
                        "automation_loop_enabled", False
                    ):
                        self.manifest.update_step(case_key, "loop", "skipped", error="loop disabled")
                        self._run_extra_branches(
                            case_key=case_key,
                            case_dir=case_dir,
                            extra_branches=extra_branches,
                            reprocess_mode=reprocess_mode,
                            resolved_selfie_provider=resolved_selfie_provider,
                            selfie_composite_mode=selfie_composite_mode,
                        )
                        return self._finalize_case(case_entry, "completed")
            if not skipped_existing_video:
                video = self.deps.video_factory()
                video.set_progress_callback(self.progress_cb)
                self._set_active_step(case_entry, "video_generate")
                self.manifest.update_step(case_key, "video_generate", "running")
                video_output_dir = case_dir / "gen-videos"
                video_output_dir.mkdir(exist_ok=True)
                # CLI parity with the GUI queue: pass the negative prompt
                # + cfg_scale + end-frame lock from the SAME config keys
                # the GUI writes (negative_prompts / cfg_scale_value /
                # lock_end_frame). The dispatcher gates each on the
                # selected model's capabilities (get_model_capabilities —
                # single source of truth), so an o3/seedance run silently
                # drops the unsupported ones exactly as the GUI does.
                output_video = self._generate_video_for_still(
                    video, final_still, video_output_dir
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

        # Step 6.5: face-track-continuity gate (Kling source).
        # Validated zero-false-positive reject filter — see
        # docs/analysis/versailles_fail_vs_pass.md. Runs before oldcam so a
        # source unlikely to pass Persona is caught BEFORE spending the
        # oldcam pass + a Persona attempt. Advisory by default
        # (automation_facetrack_required=False -> manual_review, never a
        # hard fail); degrades to a non-blocking skip if cv2/mediapipe or
        # the landmarker model is unavailable.
        if self._read_bool("automation_facetrack_enabled", True):
            ft_manifest_video = self.manifest.get_step(case_key, "video_generate").get("output")
            ft_video_path = Path(ft_manifest_video) if ft_manifest_video else None
            if not (ft_video_path and ft_video_path.exists() and ft_video_path.suffix.lower() == ".mp4"):
                if existing.video_candidate:
                    ft_video_path = Path(existing.video_candidate)
            if ft_video_path and ft_video_path.exists() and ft_video_path.suffix.lower() == ".mp4":
                from automation.face_track_gate import measure_face_track

                self._set_active_step(case_entry, "facetrack_gate")
                self.manifest.update_step(case_key, "facetrack_gate", "running")
                ft_min = self._read_float(
                    "automation_facetrack_min_pct", 96.0, min_value=0.0, max_value=100.0
                )
                ft_fps = self._read_float(
                    "automation_facetrack_sample_fps", 8.0, min_value=1.0, max_value=30.0
                )
                self._report(
                    f"face-track gate: checking Kling source "
                    f"(min {ft_min}% @ {ft_fps}fps)…", "info"
                )
                ft = measure_face_track(
                    str(ft_video_path),
                    Path(__file__).resolve().parent.parent,
                    sample_fps=ft_fps,
                    min_track_pct=ft_min,
                )
                if not ft.available:
                    self._report(
                        f"face-track gate: skipped ({ft.reason})", "info"
                    )
                    self.manifest.update_step(
                        case_key, "facetrack_gate", "skipped",
                        error=ft.reason, meta=ft.to_meta(),
                    )
                elif ft.passed:
                    self._report(
                        f"face-track gate: PASS {ft.track_pct}% "
                        f"(>= {ft_min}% threshold)", "info"
                    )
                    self.manifest.update_step(
                        case_key, "facetrack_gate", "complete",
                        output=str(ft_video_path), meta=ft.to_meta(),
                    )
                else:
                    ft_required = self._read_bool("automation_facetrack_required", False)
                    self._report(
                        f"face-track gate: FAIL {ft.track_pct}% < {ft_min}% "
                        f"-> {'failed (oldcam skipped)' if ft_required else 'manual_review'} "
                        f"(likely fails Persona — regenerate the Kling source)",
                        "warning",
                    )
                    status = "failed" if ft_required else "manual_review"
                    self.manifest.update_step(
                        case_key, "facetrack_gate", status,
                        output=str(ft_video_path), error=ft.reason,
                        meta={**ft.to_meta(), "required": ft_required},
                    )
                    self.last_case_results[case_key] = {
                        "status": status, "reason": ft.reason,
                    }
                    return self._finalize_case(case_entry, status)
            else:
                self.manifest.update_step(
                    case_key, "facetrack_gate", "skipped",
                    error="no mp4 video to gate",
                )
        else:
            self.manifest.update_step(
                case_key, "facetrack_gate", "skipped", error="facetrack gate disabled"
            )

        # Step 7-pre: rPPG-first pass (Phase E of polish/v2.3,
        # 2026-05-22). When rPPG is enabled, run injection on the raw
        # video_generate output BEFORE Oldcam so each Oldcam version
        # derives from the rPPG'd base. The injector is a graceful-skip
        # target: if the rPPG/ tool is missing, the launcher returns
        # None and we keep the raw video as Oldcam's input. The legacy
        # "rPPG on every Oldcam output" fan-out from the prior order
        # is preserved behind the ``automation_rppg_per_oldcam_fanout``
        # opt-in flag, applied in Step 8 below (default OFF).
        rppg_base_path: Optional[Path] = None
        if self._read_bool("automation_rppg_enabled", False):
            video_out = self.manifest.get_step(case_key, "video_generate").get("output")
            video_out_path = Path(video_out) if video_out else None
            if (
                video_out_path
                and video_out_path.exists()
                and video_out_path.suffix.lower() == ".mp4"
                and not is_rppg_artifact(video_out_path)
            ):
                # Resume reuse (Codex P2, round 3): the pre-pass used to be
                # untracked, so an abort right after a (minutes-long, GPU)
                # injection re-ran it from scratch on resume even though the
                # clean-named ``{stem}-rppg{ext}`` sibling sat on disk.
                existing_rppg = video_out_path.with_name(
                    f"{video_out_path.stem}-rppg{video_out_path.suffix}"
                )
                if reprocess_mode == "skip" and existing_rppg.exists():
                    rppg_base_path = existing_rppg
                    self._report(
                        f"[{case_key}] Reusing existing rPPG base: {existing_rppg.name}",
                        "info",
                    )
                    # Stamp completion NOW (before loop/oldcam finish) so
                    # finished_at ordering reflects the real chain position
                    # (CodeRabbit Major, round 4 — see the injection path
                    # below for the full reasoning).
                    if not (
                        self.manifest.get_step(case_key, "rppg").get("status") == "complete"
                        and self.manifest.get_step(case_key, "rppg").get("output") == str(existing_rppg)
                    ):
                        self.manifest.update_step(
                            case_key,
                            "rppg",
                            "complete",
                            output=str(existing_rppg),
                            meta={**self._policy_meta("rppg", True, reprocess_mode), "pre_pass": True},
                        )
                else:
                    # Track the pre-pass as the active step: the dashboard
                    # shows "rppg" instead of lying with the prior step, and
                    # the transition doubles as an abort checkpoint BEFORE
                    # the expensive injection (Codex P2, round 3). Step 8
                    # finalizes the rppg step's terminal status as before.
                    self._set_active_step(case_entry, "rppg")
                    self.manifest.update_step(case_key, "rppg", "running")
                    rppg_mode = str(self.automation.get("automation_rppg_mode") or "iterative").strip().lower()
                    iterative = rppg_mode == "iterative"
                    iterate_from_baseline = self._read_bool("automation_rppg_iterate_from_baseline", True)
                    skip_diagnosis = self._read_bool("automation_rppg_skip_diagnosis", True)
                    skip_kinematic_gate = self._read_bool("automation_rppg_skip_kinematic_gate", True)
                    keep_metrics = self._read_bool("automation_rppg_metrics_in_filename", False)
                    # _read_int is the safe coercion helper (used by every
                    # other automation_*_int key). The naive `int(... or 1)`
                    # form would crash the pipeline mid-case on a stringy
                    # config value AND silently rewrite a legitimate ``0``
                    # to the default. (Subagent HIGH on PR #52 round 3.)
                    # Default 1 from 2026-05-27 v2.6 quality-first revert.
                    landmark_stride = self._read_int(
                        "automation_rppg_landmark_stride", 1, min_value=1,
                    )
                    injected = run_rppg(
                        video_path=video_out_path,
                        repo_root=Path(__file__).resolve().parent.parent,
                        progress_cb=self.progress_cb,
                        keep_metrics=keep_metrics,
                        iterative=iterative,
                        iterate_from_baseline=iterate_from_baseline,
                        skip_diagnosis=skip_diagnosis,
                        skip_kinematic_gate=skip_kinematic_gate,
                        landmark_stride=landmark_stride,
                        # Wire verbose like every other run_rppg site — the
                        # pre-pass (the longest GPU call) silently ignored
                        # automation_verbose_logging (round-5 review).
                        verbose=self.verbose_logging,
                    )
                    if injected and injected.exists():
                        rppg_base_path = injected
                        self.logger.info(
                            "case %s rppg-first: %s -> %s",
                            case_key, video_out_path.name, injected.name,
                        )
                        # Stamp the rppg completion AT THE PRE-PASS, not in
                        # Step 8: case_is_complete_and_valid picks the final
                        # deliverable by finished_at, and Step 8's late
                        # bookkeeping ran AFTER oldcam — making the manifest
                        # claim the rPPG base finished last, which masked a
                        # deleted oldcam output as complete again
                        # (CodeRabbit Major, round 4). Step 8's "already"
                        # branch now skips the re-stamp when this record is
                        # in place.
                        self.manifest.update_step(
                            case_key,
                            "rppg",
                            "complete",
                            output=str(injected),
                            meta={**self._policy_meta("rppg", False, reprocess_mode), "pre_pass": True},
                        )
            elif (
                video_out_path
                and video_out_path.exists()
                and video_out_path.suffix.lower() == ".mp4"
                and is_rppg_artifact(video_out_path)
            ):
                # The (reused) video IS already an rPPG artifact — treat it
                # as the pre-pass result and stamp NOW, before loop/oldcam
                # finish. Leaving the stamp to Step 8 put rppg's finished_at
                # AFTER oldcam's, re-opening the masked-deleted-deliverable
                # hole for exactly this resume path (Codex P1, round 5).
                rppg_base_path = video_out_path
                _rppg_pre = self.manifest.get_step(case_key, "rppg")
                if not (
                    _rppg_pre.get("status") == "complete"
                    and _rppg_pre.get("output") == str(video_out_path)
                ):
                    self.manifest.update_step(
                        case_key,
                        "rppg",
                        "complete",
                        output=str(video_out_path),
                        meta={
                            **self._policy_meta("rppg", True, reprocess_mode),
                            "pre_pass": True,
                            "already_injected": True,
                        },
                    )

        # Step 7-pre-b: optional ping-pong loop (Phase E order:
        # Kling -> rPPG -> Loop -> Oldcam, mirroring the GUI queue).
        # Loops the rPPG'd base when present, else the raw Kling output,
        # so Oldcam derives from the looped file. Graceful-skip on any
        # failure (ffmpeg missing, encode error) — the case continues on
        # the unlooped video, mirroring the rPPG skip semantics.
        loop_path: Optional[Path] = None
        if self._read_bool("automation_loop_enabled", False):
            loop_source: Optional[Path] = None
            if rppg_base_path is not None and rppg_base_path.exists():
                loop_source = rppg_base_path
            else:
                video_out = self.manifest.get_step(case_key, "video_generate").get("output")
                candidate = Path(video_out) if video_out else None
                if candidate and candidate.exists() and candidate.suffix.lower() == ".mp4":
                    loop_source = candidate
                elif existing.video_candidate:
                    candidate = Path(existing.video_candidate)
                    if candidate.exists() and candidate.suffix.lower() == ".mp4":
                        loop_source = candidate
            if loop_source is None:
                self.manifest.update_step(
                    case_key, "loop", "skipped", error="no mp4 video to loop",
                )
            else:
                self._set_active_step(case_entry, "loop")
                self.manifest.update_step(case_key, "loop", "running")
                # skip mode reuses an existing _looped sibling (its source —
                # the rPPG base/raw video — is also reused in skip mode, so
                # it cannot be stale); overwrite/increment re-encode
                # (Gemini MED, PR #96 round 4).
                looped = create_looped_video(
                    str(loop_source),
                    suffix="_looped",
                    overwrite=(reprocess_mode != "skip"),
                    log_callback=self.progress_cb,
                )
                if looped and Path(looped).exists():
                    loop_path = Path(looped)
                    self.logger.info(
                        "case %s loop: %s -> %s", case_key, loop_source.name, loop_path.name,
                    )
                    self.manifest.update_step(
                        case_key,
                        "loop",
                        "complete",
                        output=str(loop_path),
                        meta=self._policy_meta("loop", False, reprocess_mode),
                    )
                else:
                    self.logger.warning("case %s loop failed; continuing unlooped", case_key)
                    self.manifest.update_step(
                        case_key,
                        "loop",
                        "skipped",
                        error="loop failed or ffmpeg unavailable",
                    )
        else:
            self.manifest.update_step(case_key, "loop", "skipped", error="loop disabled")

        # Step 7-pre-c: optional quality-crush pass (Phase E order:
        # Kling -> rPPG -> Loop -> Crush -> Oldcam). Re-encodes at CRF 35,
        # mimicking WhatsApp transcoding quality destruction.
        #
        # Multi-resolution (2026-06-18): ``automation_crush_resolutions`` is a
        # LIST of tiers (["720p"], ["480p"], ["720p","480p"], or []) fanned out
        # exactly like the Oldcam version list — one crushed file per tier.
        # The list is normalized + ordered highest-first by
        # merge_automation_defaults (legacy ``automation_crush_enabled``=True
        # already migrated to ["480p"] there). Graceful skip on failure — never
        # hard-fails a case unless automation_crush_required=True.
        crush_path: Optional[Path] = None  # primary (highest-res) for back-compat
        crush_paths: List[Path] = []
        crush_resolutions = self._crush_resolutions()
        if crush_resolutions:
            # Prefer looped output; fall back to rPPG base or raw Kling.
            crush_source: Optional[Path] = None
            if loop_path is not None and loop_path.exists():
                crush_source = loop_path
            elif rppg_base_path is not None and rppg_base_path.exists():
                crush_source = rppg_base_path
            else:
                _cvo = self.manifest.get_step(case_key, "video_generate").get("output")
                _cc = Path(_cvo) if _cvo else None
                if _cc and _cc.exists() and _cc.suffix.lower() == ".mp4":
                    crush_source = _cc
                elif existing.video_candidate:
                    _cc2 = Path(existing.video_candidate)
                    if _cc2.exists() and _cc2.suffix.lower() == ".mp4":
                        crush_source = _cc2
            if crush_source is None:
                self.manifest.update_step(
                    case_key, "crush", "skipped", error="no mp4 video to crush",
                )
                if self._read_bool("automation_crush_required", False):
                    raise RuntimeError(
                        f"case {case_key}: crush required but no mp4 source found"
                    )
            else:
                self._set_active_step(case_entry, "crush")
                self.manifest.update_step(case_key, "crush", "running")
                for _label in crush_resolutions:
                    _height = CRUSH_RESOLUTIONS.get(_label)
                    if not _height:
                        continue
                    crushed = crush_video(
                        str(crush_source),
                        suffix=crush_suffix(_label),
                        target_height=_height,
                        overwrite=(reprocess_mode != "skip"),
                        log_callback=self.progress_cb,
                    )
                    if crushed and Path(crushed).exists():
                        crush_paths.append(Path(crushed))
                    else:
                        # Per-tier visibility: a single tier failing is
                        # otherwise silent when another tier succeeds (gemini).
                        self.logger.warning(
                            "case %s crush tier %s failed; skipping that tier",
                            case_key, _label,
                        )
                if crush_paths:
                    crush_path = crush_paths[0]  # highest-res tier is primary
                    self.logger.info(
                        "case %s crush: %s -> %d tier(s): %s",
                        case_key, crush_source.name, len(crush_paths),
                        ", ".join(p.name for p in crush_paths),
                    )
                    self.manifest.update_step(
                        case_key,
                        "crush",
                        "complete",
                        output=str(crush_path),
                        meta={
                            **self._policy_meta("crush", False, reprocess_mode),
                            "all_outputs": [str(p) for p in crush_paths],
                        },
                    )
                elif self._read_bool("automation_crush_required", False):
                    # Required + every tier failed → hard-fail (not "continuing
                    # uncrushed"); mark the step failed so the contract is
                    # unambiguous (gemini: the warning must not imply success).
                    self.manifest.update_step(
                        case_key, "crush", "failed",
                        error="crush required but produced no output",
                    )
                    raise RuntimeError(
                        f"case {case_key}: crush required but every tier failed"
                    )
                else:
                    self.logger.warning("case %s crush failed; continuing uncrushed", case_key)
                    self.manifest.update_step(
                        case_key, "crush", "skipped", error="crush failed or ffmpeg unavailable",
                    )
        else:
            self.manifest.update_step(case_key, "crush", "skipped", error="crush disabled")
            if self._read_bool("automation_crush_required", False):
                raise RuntimeError(
                    f"case {case_key}: crush required but no resolutions selected"
                )

        # Step 7-pre-aa: optional adversarial-attack (AA) pass (Phase E order:
        # Kling -> rPPG -> Loop -> Crush -> AA -> Oldcam). Runs the aa-video
        # subproject (isolated venv) once per selected attack-pipeline; each
        # produces its own output file that then fans through Oldcam — mirrors
        # the crush-tier fan-out. AA's SOURCE is the crushed tiers when crush
        # ran, else the raw chain (loop > rPPG > raw Kling). Graceful skip on
        # failure unless automation_aa_required=True.
        aa_paths: List[Path] = []
        aa_attacks = self._aa_attacks()
        # Establish the AA/oldcam input chain (also used by the oldcam block
        # below when AA is OFF). _raw_video_path = the best non-crushed source.
        _raw_video_path: Optional[Path] = None
        if loop_path is not None and loop_path.exists():
            _raw_video_path = loop_path
        elif rppg_base_path is not None and rppg_base_path.exists():
            _raw_video_path = rppg_base_path
        else:
            _mv = self.manifest.get_step(case_key, "video_generate").get("output")
            _mvp = Path(_mv) if _mv else None
            if _mvp and _mvp.exists() and _mvp.suffix.lower() == ".mp4":
                _raw_video_path = _mvp
            elif existing.video_candidate:
                _vc = Path(existing.video_candidate)
                if _vc.exists() and _vc.suffix.lower() == ".mp4":
                    _raw_video_path = _vc

        if aa_attacks:
            # AA fans over the crushed tiers (each gets its own AA pass) so the
            # crush artefact carries into AA; falls back to the raw chain when
            # crush is off.
            aa_sources: List[Path] = []
            if crush_paths:
                aa_sources.extend(p for p in crush_paths if p.exists())
            elif _raw_video_path is not None and _raw_video_path.exists():
                aa_sources.append(_raw_video_path)
            if not aa_sources:
                self.manifest.update_step(case_key, "aa", "skipped", error="no mp4 video for AA")
                if self._read_bool("automation_aa_required", False):
                    raise RuntimeError(f"case {case_key}: AA required but no mp4 source found")
            else:
                self._set_active_step(case_entry, "aa")
                self.manifest.update_step(case_key, "aa", "running")
                _aa_strength = self._read_float("automation_aa_strength", 0.5)
                _aa_generator = str(self.automation.get("automation_aa_generator", "generic") or "")
                for _aa_src in aa_sources:
                    for _attack in aa_attacks:
                        # Abort between variants so a multi-attack / multi-source
                        # fan-out stops promptly instead of launching the next
                        # AA pass after the user aborts (codex HIGH). Each
                        # variant is an independent, re-runnable unit, so this is
                        # a safe stopping point.
                        if self.abort_event.is_set():
                            raise AutomationAbort(
                                f"aborted before AA variant {_attack} on {_aa_src.name}"
                            )
                        produced = run_aa(
                            str(_aa_src),
                            attack=_attack,
                            strength=_aa_strength,
                            generator=_aa_generator or None,
                            log_callback=self.progress_cb,
                            repo_root=Path(__file__).resolve().parent.parent,
                        )
                        if produced and Path(produced).exists():
                            aa_paths.append(Path(produced))
                        else:
                            self.logger.warning(
                                "case %s AA attack %s on %s failed; skipping that variant",
                                case_key, _attack, _aa_src.name,
                            )
                if aa_paths:
                    self.logger.info(
                        "case %s aa: %d variant(s): %s",
                        case_key, len(aa_paths), ", ".join(p.name for p in aa_paths),
                    )
                    self.manifest.update_step(
                        case_key, "aa", "complete",
                        output=str(aa_paths[0]),
                        meta={
                            **self._policy_meta("aa", False, reprocess_mode),
                            "all_outputs": [str(p) for p in aa_paths],
                        },
                    )
                elif self._read_bool("automation_aa_required", False):
                    self.manifest.update_step(
                        case_key, "aa", "failed", error="AA required but produced no output",
                    )
                    raise RuntimeError(f"case {case_key}: AA required but every attack failed")
                else:
                    self.manifest.update_step(
                        case_key, "aa", "skipped", error="AA failed or unavailable",
                    )
        else:
            self.manifest.update_step(case_key, "aa", "skipped", error="AA disabled")
            if self._read_bool("automation_aa_required", False):
                raise RuntimeError(f"case {case_key}: AA required but no attacks selected")

        # Step 7: optional oldcam pass
        if self._oldcam_active():
            # Build the list of sources for oldcam (Phase E order:
            # crush -> AA -> oldcam). Fan-out priority:
            #   1. AA variants (each AA output is its own oldcam source) — these
            #      already carry the crush artefact since AA ran on the crushed
            #      tiers. N_aa x M_oldcam finals.
            #   2. else crushed tiers (crush ran, AA off).
            #   3. else the raw chain (loop > rPPG > raw Kling).
            # The pre-crush / pre-AA originals are NEVER deleted; they simply
            # aren't re-run through oldcam (the processed variants are the
            # intended Persona-pass deliverables).
            _oldcam_sources: List[Path] = []
            if aa_paths:
                _oldcam_sources.extend(p for p in aa_paths if p.exists())
            elif crush_paths:
                _oldcam_sources.extend(p for p in crush_paths if p.exists())
            elif _raw_video_path is not None and _raw_video_path.exists():
                _oldcam_sources.append(_raw_video_path)

            # Filter to valid mp4 paths (dedup by resolved path).
            _seen_oldcam: set = set()
            _valid_sources: List[Path] = []
            for _src in _oldcam_sources:
                _key = str(_src.resolve())
                if _key not in _seen_oldcam and _src.suffix.lower() == ".mp4":
                    _seen_oldcam.add(_key)
                    _valid_sources.append(_src)

            if _valid_sources:
                self.logger.info(
                    "case %s oldcam readiness=ready sources=%d version=%s required=%s",
                    case_key, len(_valid_sources),
                    self.automation.get("automation_oldcam_version", "v12"),
                    bool(self.automation.get("automation_oldcam_required", False)),
                )
                self._set_active_step(case_entry, "oldcam")
                self.manifest.update_step(case_key, "oldcam", "running")
                # Run EVERY selected version (run_oldcam_all) for EACH source.
                # The manifest step carries one canonical ``output`` (highest
                # version from any source, back-compat) but ALL per-version
                # paths are stashed in meta["all_outputs"] so Step 8 can fan
                # rPPG over each. Plain -oldcam-vN files are kept (non-destructive).
                oldcam_all: List = []
                for _src in _valid_sources:
                    _batch = run_oldcam_all(
                        video_path=_src,
                        version_setting=self.automation.get("automation_oldcam_version", []),
                        repo_root=Path(__file__).resolve().parent.parent,
                        progress_cb=self.progress_cb,
                    )
                    oldcam_all.extend(_batch)
                oldcam_output = (
                    max(oldcam_all, key=lambda iv: _oldcam_version_key(iv[0]))[1]
                    if oldcam_all
                    else None
                )
                if oldcam_output:
                    self.logger.info("case %s oldcam output=%s", case_key, oldcam_output)
                    self.manifest.update_step(
                        case_key,
                        "oldcam",
                        "complete",
                        output=str(oldcam_output),
                        meta={
                            **self._policy_meta("oldcam", False, reprocess_mode),
                            "all_outputs": [str(p) for _v, p in oldcam_all],
                        },
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
            self.manifest.update_step(case_key, "oldcam", "skipped", error=self._oldcam_inactive_reason())

        # Step 8: optional per-Oldcam rPPG fan-out + final
        # bookkeeping. The PRIMARY rPPG injection already ran in
        # Step 7-pre above (Phase E of polish/v2.3, 2026-05-22): new
        # order is Kling -> rPPG -> Loop -> Oldcam, with rPPG
        # injected on the raw video_generate output BEFORE Oldcam so
        # every Oldcam version derives from a single rPPG'd base.
        #
        # This Step 8 block now handles two cases:
        #   1. Default flow (``automation_rppg_per_oldcam_fanout=False``):
        #      records the pre-Oldcam rPPG result via the already-
        #      injected fast path (``already`` branch) — no further
        #      injection runs.
        #   2. Opt-in legacy fan-out (``automation_rppg_per_oldcam_fanout=True``):
        #      injects each per-version Oldcam output, in addition to
        #      the pre-Oldcam base injection. Slower; preserves the
        #      old per-Oldcam file set on top of the new base.
        #
        # DEFAULT OFF; _required=False means a missing tool / failed
        # injection is a graceful skip, never a hard-fail (mirrors the
        # facetrack-gate precedent). The injector lives in the rPPG/
        # directory (committed in-tree as of Phase D, 2026-05-22),
        # invoked as an external launcher (.bat on Windows, .sh
        # elsewhere via resolve_rppg_launcher).
        if self._read_bool("automation_rppg_enabled", False):
            oldcam_step = self.manifest.get_step(case_key, "oldcam")
            oldcam_out = oldcam_step.get("output")
            oldcam_all = list((oldcam_step.get("meta") or {}).get("all_outputs") or [])
            video_out = self.manifest.get_step(case_key, "video_generate").get("output")
            required = self._read_bool("automation_rppg_required", False)
            keep_metrics = self._read_bool("automation_rppg_metrics_in_filename", False)
            # Iterative+companion flags. PR #43 / friend feedback: the
            # rPPG injector's iterative mode is mandatory for production
            # — the initial single-shot injection rarely lands at the
            # optimal strength. Default ON via automation_rppg_mode.
            rppg_mode = str(self.automation.get("automation_rppg_mode") or "iterative").strip().lower()
            iterative = rppg_mode == "iterative"
            iterate_from_baseline = self._read_bool("automation_rppg_iterate_from_baseline", True)
            skip_diagnosis = self._read_bool("automation_rppg_skip_diagnosis", True)
            skip_kinematic_gate = self._read_bool("automation_rppg_skip_kinematic_gate", True)
            # _read_int safety wrapper (see Step 6 site above) —
            # subagent HIGH on PR #52 round 3. Default reverted 3 -> 1
            # in fix/step0-composite-and-rppg-v2.5 (snapshot-race
            # regression — see automation/config.py for full reasoning).
            landmark_stride = self._read_int(
                "automation_rppg_landmark_stride", 1, min_value=1,
            )

            # Phase E of polish/v2.3 (2026-05-22): the BASE rPPG pass
            # already ran BEFORE Step 7 (Oldcam), so each oldcam output
            # is built on top of the rPPG'd base — the base injection
            # need not be repeated here. The legacy "fan rPPG over
            # every Oldcam output" path is preserved behind the
            # ``automation_rppg_per_oldcam_fanout`` opt-in flag below
            # (default OFF; user-direction 2026-05-22).
            #
            # When the flag is OFF (default), candidates is just the
            # base injection target — but rPPG already ran on it in
            # the pre-Oldcam pass, so the file on disk is an rPPG
            # artifact and the existing "drop already-injected" guard
            # will move it into ``already`` and record it as the final
            # deliverable. That is the intended behaviour: one rPPG'd
            # base, every Oldcam version built on it, no per-Oldcam
            # fan-out.
            #
            # Back-compat note (still applies): a manifest whose
            # oldcam step completed BEFORE meta["all_outputs"] existed
            # has only the legacy single ``oldcam.output``. We include
            # ``oldcam_out`` in the source list whenever
            # ``all_outputs`` is empty — the seen-set dedups it if it
            # also happens to equal a video/all_outputs entry.
            per_oldcam_fanout = self._read_bool(
                "automation_rppg_per_oldcam_fanout", False
            )
            oldcam_sources = oldcam_all if oldcam_all else ([oldcam_out] if oldcam_out else [])
            candidates: List[Path] = []
            seen: set = set()
            # Phase E: substitute the rPPG'd base from the pre-Oldcam
            # pass so the already-injected guard recognises it as a
            # deliverable WITHOUT re-running injection. Without this
            # substitution, the candidate list would contain the raw
            # ``video_out`` (which is NOT an rPPG artifact) and the
            # downstream loop would inject it again, producing the
            # same ``<stem>-rppg.mp4`` output but wasting one full
            # iterative-injection cycle.
            base_candidate = (
                str(rppg_base_path) if rppg_base_path and rppg_base_path.exists()
                else video_out
            )
            # Always include the base candidate. Include oldcam_sources
            # ONLY when the legacy per-Oldcam fan-out is opted-in.
            raw_sources = [base_candidate]
            if per_oldcam_fanout:
                raw_sources.extend(oldcam_sources)
            for raw in raw_sources:
                if not raw:
                    continue
                p = Path(raw)
                key = str(p)
                if key in seen or not p.exists():
                    continue
                seen.add(key)
                candidates.append(p)

            # Drop any candidate that is ALREADY an rPPG artifact —
            # re-injecting would double-inject (-rppg-rppg) and compound
            # the pulse out of the non-negotiable sub-perceptual range.
            # (Codex P2, PR #39.) An already-injected file IS a final
            # deliverable; it just doesn't get re-run.
            already = [p for p in candidates if is_rppg_artifact(p)]
            to_inject = [p for p in candidates if not is_rppg_artifact(p)]

            if not candidates:
                self.logger.warning("case %s rppg readiness=not-ready required=%s", case_key, required)
                self.manifest.update_step(
                    case_key,
                    "rppg",
                    "failed" if required else "skipped",
                    error="no input video for rPPG",
                    meta={"required": required},
                )
                if required:
                    return self._finalize_case(case_entry, "failed")
            elif not to_inject:
                # Every candidate is already injected — nothing to do but
                # record the (highest/last) one as the final deliverable.
                final = str(already[-1])
                self.logger.info(
                    "case %s rppg: all %d candidate(s) already injected; "
                    "recording %s",
                    case_key,
                    len(already),
                    already[-1].name,
                )
                _rppg_step = self.manifest.get_step(case_key, "rppg")
                if _rppg_step.get("status") == "complete" and _rppg_step.get("output") == final:
                    # The pre-pass already stamped this exact completion.
                    # Re-stamping here would push rppg's finished_at AFTER
                    # oldcam's and make case_is_complete_and_valid treat the
                    # BASE as the final deliverable again (CodeRabbit Major,
                    # round 4 — the finished_at ordering must reflect real
                    # chain position).
                    self.logger.info("case %s rppg pre-pass record kept (no re-stamp)", case_key)
                else:
                    self.manifest.update_step(
                        case_key,
                        "rppg",
                        "complete",
                        output=final,
                        meta={**self._policy_meta("rppg", True, reprocess_mode), "already_injected": True},
                    )
                self._run_powerset_extras(
                    case_key=case_key,
                    rppg_base_path=rppg_base_path,
                    reprocess_mode=reprocess_mode,
                )
                self._run_extra_branches(
                    case_key=case_key,
                    case_dir=case_dir,
                    extra_branches=extra_branches,
                    reprocess_mode=reprocess_mode,
                    resolved_selfie_provider=resolved_selfie_provider,
                    selfie_composite_mode=selfie_composite_mode,
                )
                return self._finalize_case(case_entry, "completed")
            else:
                self.logger.info(
                    "case %s rppg readiness=ready required=%s fan-out=%d",
                    case_key,
                    required,
                    len(to_inject),
                )
                self._set_active_step(case_entry, "rppg")
                self.manifest.update_step(case_key, "rppg", "running")
                # Track success/failure PER candidate. ``to_inject`` is
                # ordered base -> oldcam ascending. A naive "collect
                # successes, headline produced[-1]" silently masks a
                # partial fan-out failure: if the base injects but an
                # oldcam injection fails, produced[-1] becomes the base
                # *-rppg and the case is marked complete exposing a
                # non-oldcam clip — and with rppg_required=true a partial
                # failure is reported as success. (Codex P2, PR #40.)
                produced: List[str] = []          # all successful outputs
                failed_inputs: List[str] = []     # candidates with no output
                # Map src -> output so the headline can prefer the
                # HIGHEST oldcam that actually succeeded (not blindly the
                # last attempted). to_inject order == priority order.
                produced_for: dict = {}
                # Phase E (2026-05-22): seed ``produced`` with the rPPG
                # output from the pre-Oldcam pass when it exists. The
                # base rPPG'd file is a legitimate deliverable but
                # already exists on disk (handled via the ``already``
                # branch above when the only candidate IS the base).
                # When per-Oldcam fan-out is on, including it in
                # ``produced`` keeps ``all_outputs`` complete and lets
                # downstream code see the full set of injected files.
                if rppg_base_path is not None and rppg_base_path.exists():
                    produced.append(str(rppg_base_path))
                for src in to_inject:
                    out = run_rppg(
                        video_path=src,
                        repo_root=Path(__file__).resolve().parent.parent,
                        progress_cb=self.progress_cb,
                        keep_metrics=keep_metrics,
                        iterative=iterative,
                        iterate_from_baseline=iterate_from_baseline,
                        skip_diagnosis=skip_diagnosis,
                        skip_kinematic_gate=skip_kinematic_gate,
                        landmark_stride=landmark_stride,
                        verbose=self.verbose_logging,
                    )
                    if out and out.exists():
                        produced.append(str(out))
                        produced_for[str(src)] = str(out)
                    else:
                        failed_inputs.append(Path(src).name)

                partial = bool(produced) and bool(failed_inputs)
                if produced and not (required and failed_inputs):
                    # Headline = the rPPG of the LAST candidate that
                    # actually succeeded (candidates are base -> oldcam
                    # ascending, so the last success is the
                    # highest-priority real deliverable — never a clip
                    # that silently skipped its oldcam injection).
                    # Subagent CRITICAL on 69dee05: when Phase E's
                    # pre-Oldcam rPPG succeeded AND every per-Oldcam
                    # fan-out attempt failed, ``produced`` is non-empty
                    # (seeded with ``rppg_base_path`` above) but
                    # ``produced_for`` is empty (only fan-out
                    # injections populate it). The bare ``next(gen)``
                    # then raised an uncaught ``StopIteration`` and
                    # crashed the case. Fall back to ``produced[-1]``
                    # (the seeded base, which IS the right deliverable
                    # for that scenario: every Oldcam fanout fail
                    # leaves the rPPG'd base as the only real output).
                    headline = next(
                        (
                            produced_for[str(s)]
                            for s in reversed(to_inject)
                            if str(s) in produced_for
                        ),
                        produced[-1],
                    )
                    status_note = ""
                    if partial:
                        status_note = (
                            f" (PARTIAL: {len(failed_inputs)} of "
                            f"{len(to_inject)} candidate(s) failed: "
                            f"{', '.join(failed_inputs)})"
                        )
                    self.logger.info(
                        "case %s rppg outputs=%d headline=%s%s",
                        case_key,
                        len(produced),
                        Path(headline).name,
                        status_note,
                    )
                    self.manifest.update_step(
                        case_key,
                        "rppg",
                        "complete",
                        output=headline,
                        meta={
                            **self._policy_meta("rppg", False, reprocess_mode),
                            "all_outputs": produced,
                            "failed_inputs": failed_inputs,
                            "partial": partial,
                        },
                    )
                else:
                    # Either nothing was produced, OR required=true and at
                    # least one candidate failed — a required fan-out must
                    # be ALL-or-fail so a missing oldcam deliverable is
                    # never reported as success.
                    if not produced:
                        err = "rPPG injection produced no output"
                    else:
                        err = (
                            f"rPPG required but {len(failed_inputs)} of "
                            f"{len(to_inject)} candidate(s) failed: "
                            f"{', '.join(failed_inputs)}"
                        )
                    self.logger.warning(
                        "case %s rppg failed required=%s (%s)",
                        case_key,
                        required,
                        err,
                    )
                    self.manifest.update_step(
                        case_key,
                        "rppg",
                        "failed" if required else "skipped",
                        error=err,
                        meta={
                            **self._policy_meta("rppg", False, reprocess_mode),
                            "required": required,
                            "all_outputs": produced,
                            "failed_inputs": failed_inputs,
                        },
                    )
                    if required:
                        return self._finalize_case(case_entry, "failed")
        else:
            self.manifest.update_step(case_key, "rppg", "skipped", error="rPPG disabled")

        self._run_powerset_extras(
            case_key=case_key,
            rppg_base_path=rppg_base_path,
            reprocess_mode=reprocess_mode,
        )
        self._run_extra_branches(
            case_key=case_key,
            case_dir=case_dir,
            extra_branches=extra_branches,
            reprocess_mode=reprocess_mode,
            resolved_selfie_provider=resolved_selfie_provider,
            selfie_composite_mode=selfie_composite_mode,
        )
        return self._finalize_case(case_entry, "completed")
