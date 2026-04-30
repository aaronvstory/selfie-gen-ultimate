from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA_VERSION = 1
STEP_NAMES = [
    "front_expand",
    "extract_portrait",
    "selfie_generate",
    "similarity_gate",
    "selfie_expand",
    "video_generate",
    "oldcam",
]
STEP_STATUSES = {"pending", "running", "complete", "failed", "manual_review", "skipped", "pending_not_implemented"}
MANIFEST_CONFIG_FINGERPRINT_KEYS = (
    "automation_front_names",
    "automation_manifest_name",
    "automation_reprocess_mode",
    "automation_front_expand_mode",
    "automation_front_expand_provider",
    "automation_selfie_expand_mode",
    "automation_selfie_expand_provider",
)


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


def _build_config_fingerprint(config_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {key: config_snapshot.get(key) for key in MANIFEST_CONFIG_FINGERPRINT_KEYS if key in config_snapshot}


@dataclass
class AutomationManifest:
    manifest_path: Path
    data: Dict[str, Any]

    @classmethod
    def create_or_load(cls, manifest_path: Path, root_dir: Path, config_snapshot: Dict[str, Any]) -> "AutomationManifest":
        resolved_root = str(root_dir.resolve())
        desired_fingerprint = _build_config_fingerprint(config_snapshot)
        if manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except JSONDecodeError as exc:
                cls._backup_invalid_manifest(manifest_path, f"invalid json: {exc}")
            except OSError as exc:
                raise ValueError(f"Manifest load failed at {manifest_path}: {exc}") from exc

            if not isinstance(loaded, dict):
                cls._backup_invalid_manifest(manifest_path, f"manifest root type invalid: {type(loaded).__name__}")
            if loaded.get("schema_version") != SCHEMA_VERSION:
                got_version = loaded.get("schema_version")
                cls._backup_invalid_manifest(
                    manifest_path,
                    f"schema_version mismatch: got {got_version!r}, expected {SCHEMA_VERSION}",
                )

            loaded_root = str(Path(loaded.get("root_dir", "")).resolve())
            if loaded_root != resolved_root:
                raise ValueError(
                    f"Manifest root mismatch at {manifest_path}: manifest={loaded_root!r}, requested={resolved_root!r}"
                )

            loaded_fingerprint = _build_config_fingerprint(loaded.get("config_snapshot", {}))
            if loaded_fingerprint != desired_fingerprint:
                raise ValueError(
                    f"Manifest config fingerprint mismatch at {manifest_path}: "
                    f"manifest={loaded_fingerprint!r}, requested={desired_fingerprint!r}"
                )
            return cls(manifest_path=manifest_path, data=loaded)

        created = {
            "schema_version": SCHEMA_VERSION,
            "root_dir": resolved_root,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "config_snapshot": config_snapshot,
            "cases": {},
        }
        inst = cls(manifest_path=manifest_path, data=created)
        inst.save_atomic()
        return inst

    @staticmethod
    def _backup_invalid_manifest(manifest_path: Path, reason: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = manifest_path.with_suffix(manifest_path.suffix + f".corrupt.{timestamp}")
        os.replace(manifest_path, backup_path)
        raise ValueError(f"Manifest invalid at {manifest_path}: {reason}. Backed up to {backup_path}.")

    @staticmethod
    def _backup_invalid_manifest_no_raise(manifest_path: Path, reason: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = manifest_path.with_suffix(manifest_path.suffix + f".corrupt.{timestamp}")
        os.replace(manifest_path, backup_path)

    @classmethod
    def load_if_exists(cls, manifest_path: Path) -> Optional["AutomationManifest"]:
        if not manifest_path.exists():
            return None
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            cls._backup_invalid_manifest_no_raise(manifest_path, f"invalid json: {exc}")
            return None
        except OSError:
            return None
        if not isinstance(loaded, dict) or loaded.get("schema_version") != SCHEMA_VERSION:
            got_version = loaded.get("schema_version") if isinstance(loaded, dict) else type(loaded).__name__
            cls._backup_invalid_manifest_no_raise(
                manifest_path,
                f"schema_version mismatch: got {got_version!r}, expected {SCHEMA_VERSION}",
            )
            return None
        return cls(manifest_path=manifest_path, data=loaded)

    def get_step(self, relative_key: str, step_name: str) -> Dict[str, Any]:
        return self.data.get("cases", {}).get(relative_key, {}).get("steps", {}).get(step_name, {})

    def save_atomic(self) -> None:
        self.data["updated_at"] = now_iso()
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.manifest_path.with_suffix(self.manifest_path.suffix + ".tmp")
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2)
        os.replace(temp_path, self.manifest_path)

    def ensure_case(self, relative_key: str, case_dir: Path, front_path: Path) -> Dict[str, Any]:
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
        case_entry = self.data.get("cases", {}).get(relative_key)
        if not case_entry:
            return False
        if case_entry.get("status") != "complete":
            return False
        final_output = (
            case_entry.get("steps", {}).get("oldcam", {}).get("output")
            or case_entry.get("steps", {}).get("video_generate", {}).get("output")
        )
        if not final_output:
            return False
        return Path(final_output).exists()
