from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = 1
STEP_NAMES = [
    "front_expand",
    "extract_portrait",
    "selfie_generate",
    "similarity_gate",
    "selfie_expand",
    "video_generate",
    "facetrack_gate",
    # Post-processing order is Kling -> rPPG -> Loop -> Oldcam (Phase E,
    # mirrored from the GUI queue). "loop" (ping-pong, 2026-06-11) sits
    # between the rPPG-first injection and the oldcam fan-out; ensure_case
    # setdefault()s it into pre-loop manifests.
    "loop",
    "oldcam",
    "rppg",
]
STEP_STATUSES = {"pending", "running", "complete", "failed", "manual_review", "skipped", "pending_not_implemented"}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_step_state() -> Dict[str, Any]:
    return {
        "status": "pending",
        "output": None,
        "provider": None,
        "margins": None,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "meta": {},
    }


def _new_case_state(case_dir: str, front_path: str) -> Dict[str, Any]:
    return {
        "case_dir": case_dir,
        "front_path": front_path,
        "status": "pending",
        "steps": {name: _new_step_state() for name in STEP_NAMES},
        "outputs": {},
        "errors": [],
        "updated_at": now_iso(),
    }


def _collision_free_backup_path(manifest_path: Path, kind: str) -> Path:
    """Backup name that can never clobber an earlier backup.

    Second-resolution timestamps alone let two backups in the same second
    silently overwrite each other via os.replace (Codex P2, PR #96 round
    4 — e.g. a double create_fresh in quick succession). A numeric suffix
    loop keeps every backup.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = manifest_path.with_suffix(manifest_path.suffix + f".{kind}.{timestamp}")
    index = 1
    while candidate.exists():
        candidate = manifest_path.with_suffix(
            manifest_path.suffix + f".{kind}.{timestamp}-{index}"
        )
        index += 1
    return candidate


# Run-SCOPE / metadata keys deliberately EXCLUDED from the config fingerprint.
# The fingerprint exists to answer ONE question: "would re-running produce
# DIFFERENT outputs for a case than the ones this manifest already recorded?"
# These keys control how much of the batch runs per invocation (max cases,
# reprocess policy), logging verbosity, discovery scope (which folders are
# FOUND — not what a processed case produces), or pure bookkeeping (the
# recommended-defaults version stamp). Fingerprinting them forced a
# back-up-and-recreate prompt on every trivial change (user, PR #96 round 6:
# flipping max-cases 5 -> 1 demanded a manifest rebuild per run).
# The exclusion applies to BOTH sides of the comparison (loaded manifest AND
# requested config flow through this builder), so manifests that RECORDED
# these keys before the exclusion existed stay valid too.
_FINGERPRINT_EXCLUDED_KEYS = frozenset(
    {
        "automation_max_cases_per_run",
        "automation_reprocess_mode",
        "automation_allow_reprocess",
        "automation_verbose_logging",
        "automation_recommended_defaults_version",
        "automation_root_folder",  # compared separately as the manifest root
        "automation_front_names",  # discovery scope only
        "automation_front_globs",  # discovery scope only
    }
)


def _build_config_fingerprint(config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    automation_keys = sorted(
        key
        for key in config_snapshot
        if key.startswith("automation_") and key not in _FINGERPRINT_EXCLUDED_KEYS
    )
    fingerprint = {key: config_snapshot.get(key) for key in automation_keys}
    # automation_oldcam_version changed representation (legacy single string
    # "v24"/"all" -> canonical list ["v24"]). Normalize on BOTH sides of the
    # fingerprint comparison (loaded manifest AND requested config flow
    # through here) so the representation change alone never invalidates an
    # old manifest — only a REAL selection change does. Cycle-safe import
    # (oldcam.py has no automation-internal imports).
    if "automation_oldcam_version" in fingerprint:
        from automation.oldcam import normalize_oldcam_versions

        fingerprint["automation_oldcam_version"] = normalize_oldcam_versions(
            fingerprint["automation_oldcam_version"]
        )
    return fingerprint


def _new_manifest_payload(root_dir: Path, config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "root_dir": str(root_dir.resolve()),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "config_snapshot": config_snapshot,
        "cases": {},
    }


@dataclass
class AutomationManifest:
    manifest_path: Path
    data: Dict[str, Any]
    # Guards data mutation + the atomic write against the CLI live
    # dashboard's reader thread (the worker mutates cases while the UI
    # thread renders). RLock: update_step/ensure_case call save_atomic
    # internally. Excluded from repr/compare (not part of manifest value).
    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False, compare=False
    )

    @property
    def lock(self) -> threading.RLock:
        """Public handle for callers that mutate ``data`` directly (the
        pipeline's case-status writes). BOTH sides of every read/write pair
        must hold this lock — locking only the snapshot reader while the
        worker writes bare gives the appearance of thread safety without
        the substance (code-review HIGH, PR #96)."""
        return self._lock

    @classmethod
    def create_or_load(cls, manifest_path: Path, root_dir: Path, config_snapshot: Dict[str, Any]) -> "AutomationManifest":
        resolved_root = str(root_dir.resolve())
        desired_fingerprint = _build_config_fingerprint(config_snapshot)
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (JSONDecodeError, UnicodeDecodeError) as exc:
                cls._backup_invalid_manifest_no_raise(manifest_path, f"invalid json: {exc}")
                created = _new_manifest_payload(root_dir, config_snapshot)
                inst = cls(manifest_path=manifest_path, data=created)
                inst.save_atomic()
                return inst
            except OSError as exc:
                raise ValueError(f"Manifest load failed at {manifest_path}: {exc}") from exc

            if not isinstance(loaded, dict):
                cls._backup_invalid_manifest_no_raise(
                    manifest_path, f"manifest root type invalid: {type(loaded).__name__}"
                )
                created = _new_manifest_payload(root_dir, config_snapshot)
                inst = cls(manifest_path=manifest_path, data=created)
                inst.save_atomic()
                return inst
            if loaded.get("schema_version") != SCHEMA_VERSION:
                got_version = loaded.get("schema_version")
                cls._backup_invalid_manifest_no_raise(
                    manifest_path,
                    f"schema_version mismatch: got {got_version!r}, expected {SCHEMA_VERSION}",
                )
                created = _new_manifest_payload(root_dir, config_snapshot)
                inst = cls(manifest_path=manifest_path, data=created)
                inst.save_atomic()
                return inst

            loaded_root = str(Path(loaded.get("root_dir", "")).resolve())
            if loaded_root != resolved_root:
                # A manifest's authoritative identity is the FOLDER IT
                # PHYSICALLY LIVES IN, not the absolute-path STRING it
                # recorded at creation time. That string is OS- and
                # location-specific: a manifest written on Windows records
                # root_dir="F:\\...\\Batch_04", which Path().resolve() on
                # macOS mangles into "<cwd>/F:\\...\\Batch_04" (a backslash
                # path is not absolute on POSIX) — a guaranteed mismatch
                # even when the file sits in EXACTLY the requested folder.
                # The same happens when a folder is moved/renamed on one OS.
                # When the manifest resides in the requested root, that
                # stale root_dir is just metadata: rebase it to the live
                # path and continue (cross-OS resume fix, 2026-06-13). Only
                # a manifest loaded for a root it does NOT live in is a
                # genuinely misplaced manifest -> hard error.
                manifest_home = str(manifest_path.parent.resolve())
                if manifest_home != resolved_root:
                    raise ValueError(
                        f"Manifest root mismatch at {manifest_path}: manifest={loaded_root!r}, requested={resolved_root!r}"
                    )
                loaded["root_dir"] = resolved_root
                cls(manifest_path=manifest_path, data=loaded).save_atomic()

            loaded_fingerprint = _build_config_fingerprint(loaded.get("config_snapshot", {}))
            # Backward compatibility (Codex P1/P2, PR #39): a purely-
            # additive new automation_* key (e.g. automation_rppg_*) must
            # NOT invalidate manifests written before it existed *when the
            # requested value is still the default* — that run is
            # behaviour-identical. But if the user EXPLICITLY opts the new
            # feature in (requested value != default, e.g.
            # automation_rppg_enabled=true on a pre-rPPG corpus), the case
            # MUST reprocess so the new step actually runs — otherwise
            # skip_completed skips it as "complete" on the stale pre-rPPG
            # output and the opted-in feature silently never executes.
            from automation.config import AUTOMATION_DEFAULTS  # cycle-safe (config does not import manifest)

            conflicting = {}
            # 1. Keys the OLD manifest recorded: any value change is a
            #    conflict (original change-detection guarantee).
            for key in loaded_fingerprint:
                if key not in desired_fingerprint or loaded_fingerprint[key] != desired_fingerprint[key]:
                    conflicting[key] = (loaded_fingerprint[key], desired_fingerprint.get(key))
            # 2. NEW keys absent from the old manifest: tolerated ONLY when
            #    the requested value equals the documented default
            #    (behaviour-preserving). A non-default requested value =
            #    explicit opt-in => force reprocess.
            for key, desired_val in desired_fingerprint.items():
                if key in loaded_fingerprint:
                    continue
                default_val = AUTOMATION_DEFAULTS.get(key, object())
                if desired_val != default_val:
                    conflicting[key] = ("<absent: pre-feature manifest>", desired_val)
            if conflicting:
                raise ValueError(
                    f"Manifest config fingerprint mismatch at {manifest_path}: "
                    f"conflicting keys (manifest vs requested)={conflicting!r}"
                )
            return cls(manifest_path=manifest_path, data=loaded)

        created = _new_manifest_payload(root_dir, config_snapshot)
        inst = cls(manifest_path=manifest_path, data=created)
        inst.save_atomic()
        return inst

    @classmethod
    def create_fresh(cls, manifest_path: Path, root_dir: Path, config_snapshot: Dict[str, Any]) -> "AutomationManifest":
        """Force-create a new manifest, backing up any existing one.

        Used when the caller has INTENTIONALLY changed the run identity (an
        explicit fingerprinted override like --oldcam-version) and a stale
        manifest would otherwise fail create_or_load with a fingerprint
        mismatch. The old manifest is renamed aside (``.superseded.<ts>``) so it
        is recoverable, then a fresh manifest is written with the requested
        snapshot.
        """
        if manifest_path.exists():
            backup_path = _collision_free_backup_path(manifest_path, "superseded")
            os.replace(manifest_path, backup_path)
        created = _new_manifest_payload(root_dir, config_snapshot)
        inst = cls(manifest_path=manifest_path, data=created)
        inst.save_atomic()
        return inst

    @staticmethod
    def _backup_invalid_manifest(manifest_path: Path, reason: str) -> None:
        backup_path = _collision_free_backup_path(manifest_path, "corrupt")
        os.replace(manifest_path, backup_path)
        raise ValueError(f"Manifest invalid at {manifest_path}: {reason}. Backed up to {backup_path}.")

    @staticmethod
    def _backup_invalid_manifest_no_raise(manifest_path: Path, reason: str) -> None:
        backup_path = _collision_free_backup_path(manifest_path, "corrupt")
        os.replace(manifest_path, backup_path)

    @classmethod
    def load_if_exists(
        cls, manifest_path: Path, *, read_only: bool = False
    ) -> Optional["AutomationManifest"]:
        """Load the manifest or return None.

        ``read_only=True`` makes invalid-manifest handling NON-mutating
        (no .corrupt rename): preview surfaces (scan, dry run) promise to
        leave the tree untouched — a dry run quietly renaming the manifest
        breaks that promise (Codex P2, PR #96 round 4). Real load paths
        keep the rename-aside recovery behavior.
        """
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            if not read_only:
                cls._backup_invalid_manifest_no_raise(manifest_path, f"invalid json: {exc}")
            return None
        except OSError:
            return None
        if not isinstance(loaded, dict) or loaded.get("schema_version") != SCHEMA_VERSION:
            got_version = loaded.get("schema_version") if isinstance(loaded, dict) else type(loaded).__name__
            if not read_only:
                cls._backup_invalid_manifest_no_raise(
                    manifest_path,
                    f"schema_version mismatch: got {got_version!r}, expected {SCHEMA_VERSION}",
                )
            return None
        return cls(manifest_path=manifest_path, data=loaded)

    def get_step(self, relative_key: str, step_name: str) -> Dict[str, Any]:
        return self.data.get("cases", {}).get(relative_key, {}).get("steps", {}).get(step_name, {})

    def save_atomic(self) -> None:
        with self._lock:
            self.data["updated_at"] = now_iso()
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.manifest_path.with_suffix(self.manifest_path.suffix + ".tmp")
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2)
            os.replace(temp_path, self.manifest_path)

    def ensure_case(self, relative_key: str, case_dir: Path, front_path: Path) -> Dict[str, Any]:
        with self._lock:
            cases = self.data.setdefault("cases", {})
            if relative_key not in cases:
                cases[relative_key] = _new_case_state(str(case_dir), str(front_path))
            case_entry = cases[relative_key]
            case_entry["case_dir"] = str(case_dir)
            case_entry["front_path"] = str(front_path)
            case_entry.setdefault("steps", {})
            for step_name in STEP_NAMES:
                case_entry["steps"].setdefault(step_name, _new_step_state())
            case_entry.setdefault("outputs", {})
            case_entry.setdefault("errors", [])
            case_entry["updated_at"] = now_iso()
            return case_entry

    def reset_case_for_new_front(self, relative_key: str, case_dir: Path, front_path: Path) -> None:
        """Wipe a case's recorded state because its FRONT IMAGE changed.

        A front_names/front_globs change can re-select a DIFFERENT file
        inside the same case folder; everything previously recorded for the
        case was generated from another source image, so skipping or
        resuming it would silently deliver wrong-source outputs. This is the
        strictly-better PER-CASE replacement for the old whole-manifest
        fingerprint rebuild on discovery-key changes (adversarial review M1,
        PR #96 round 7)."""
        with self._lock:
            cases = self.data.setdefault("cases", {})
            cases[relative_key] = _new_case_state(str(case_dir), str(front_path))
            self.save_atomic()

    def snapshot_statuses(self, case_keys: List[str]) -> Dict[str, Dict[str, Any]]:
        """Thread-safe per-case snapshot for the live dashboard's reader
        thread: status, active_step, and the similarity-gate score, COPIED
        under the lock so the renderer never iterates dicts the pipeline
        worker is mutating (the partial-panel bug, 2026-06-11)."""
        with self._lock:
            # Corrupted/hand-edited manifest: a non-dict root or "cases"
            # value must degrade to empty, not AttributeError the reader
            # thread (Gemini HIGH, PR #96 round 10).
            cases = self.data.get("cases") if isinstance(self.data, dict) else None
            if not isinstance(cases, dict):
                cases = {}
            snapshot: Dict[str, Dict[str, Any]] = {}
            for key in case_keys:
                entry = cases.get(key)
                if not isinstance(entry, dict):
                    # Corrupted/hand-edited manifest: never crash the
                    # dashboard render thread (Gemini MED, PR #96 round 5).
                    snapshot[key] = {"status": "pending", "active_step": None, "similarity": None}
                    continue
                steps = entry.get("steps")
                gate = steps.get("similarity_gate") if isinstance(steps, dict) else None
                meta = gate.get("meta") if isinstance(gate, dict) else None
                sim = meta.get("score") if isinstance(meta, dict) else None
                snapshot[key] = {
                    "status": str(entry.get("status", "pending")),
                    "active_step": entry.get("active_step"),
                    "similarity": sim,
                }
            return snapshot

    def update_step(
        self,
        relative_key: str,
        step_name: str,
        status: str,
        *,
        output: Optional[str] = None,
        provider: Optional[str] = None,
        margins: Optional[Dict[str, int]] = None,
        error: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if step_name not in STEP_NAMES:
            raise ValueError(f"Unknown step: {step_name}")
        if status not in STEP_STATUSES:
            raise ValueError(f"Invalid step status: {status}")
        with self._lock:
            case_entry = self.data["cases"][relative_key]
            step = case_entry["steps"][step_name]

            if status == "running" and not step.get("started_at"):
                step["started_at"] = now_iso()
            if status in {"complete", "failed", "manual_review", "skipped", "pending_not_implemented"}:
                step["finished_at"] = now_iso()

            step["status"] = status
            step["output"] = output
            step["provider"] = provider
            step["margins"] = margins
            step["error"] = error
            if meta is not None:
                step["meta"] = meta

            if output:
                case_entry["outputs"][step_name] = output
            if error:
                case_entry["errors"].append({"step": step_name, "error": error, "at": now_iso()})
            case_entry["updated_at"] = now_iso()
            self.save_atomic()

    def case_is_complete_and_valid(self, relative_key: str) -> bool:
        # Lock discipline: the worker thread calls this while the dashboard
        # reader thread holds the lock for snapshot_statuses — BOTH sides of
        # every read/write pair must hold it (entire-branch review MED; the
        # lock is an RLock, so nested acquisition is safe).
        with self._lock:
            # isinstance guards: a corrupted/hand-edited manifest may carry a
            # non-dict "cases" or case entry (Gemini HIGH, PR #96 round 10).
            cases = self.data.get("cases") if isinstance(self.data, dict) else None
            case_entry = cases.get(relative_key) if isinstance(cases, dict) else None
            if not isinstance(case_entry, dict):
                return False
            if case_entry.get("status") != "complete":
                return False
            steps = case_entry.get("steps")
            if not isinstance(steps, dict):
                # Same corrupted-manifest class as the guards above: a
                # null/str "steps" must read as empty, not AttributeError
                # (Gemini HIGH, PR #96 round 11).
                steps = {}

            def _step(name: str) -> Dict[str, Any]:
                value = steps.get(name)
                return value if isinstance(value, dict) else {}

            # The FINAL deliverable belongs to whichever post-process stage
            # finished LAST — and that is order-dependent: the Phase E default
            # runs Kling -> rPPG(base) -> Loop -> Oldcam (oldcam output is
            # final), while the legacy per-oldcam fan-out runs rPPG last (the
            # injected file is final, the PR #39 contract). A static stage
            # preference breaks one or the other: validating the rppg BASE
            # under Phase E masked a deleted oldcam output as "complete"
            # (Codex P1, PR #96 round 4). finished_at (ISO-8601, lexically
            # chronological) is the order-independent truth — update_step stamps
            # it on every terminal status, so real manifests always carry it.
            # Ties / missing timestamps keep the historical PR #39 preference
            # (rppg > loop > oldcam) so legacy states behave as before.
            candidates = []
            for preference, stage in enumerate(("oldcam", "loop", "rppg")):
                stage_step = _step(stage)
                if stage_step.get("status") == "complete" and stage_step.get("output"):
                    candidates.append(
                        (str(stage_step.get("finished_at") or ""), preference, stage_step["output"])
                    )
            if candidates:
                final_output = max(candidates)[2]
            else:
                final_output = (
                    _step("oldcam").get("output")
                    or _step("loop").get("output")
                    or _step("video_generate").get("output")
                )
        if not final_output:
            return False
        return Path(final_output).exists()
