from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

from path_utils import get_app_dir


def resolve_automation_log_path(config: Dict[str, Any], automation_root_folder: Optional[str]) -> Path:
    if automation_root_folder:
        return Path(automation_root_folder) / "automation_debug.log"
    return Path(get_app_dir()) / "automation_debug.log"


def key_status(value: Any) -> str:
    return "set" if str(value or "").strip() else "missing"


def build_safe_config_snapshot(config: Dict[str, Any], automation_root_folder: Optional[str]) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "automation_root_folder": automation_root_folder or "",
        "falai_api_key": key_status(config.get("falai_api_key")),
        "bfl_api_key": key_status(config.get("bfl_api_key")),
        "current_model": config.get("current_model"),
        "model_display_name": config.get("model_display_name"),
        "current_prompt_slot": config.get("current_prompt_slot"),
    }
    for key, value in config.items():
        if str(key).startswith("automation_"):
            snapshot[key] = value
    return snapshot


def create_automation_logger(config: Dict[str, Any], automation_root_folder: Optional[str]) -> tuple[logging.Logger, Path]:
    log_path = resolve_automation_log_path(config, automation_root_folder)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"automation.{log_path.resolve()}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger, log_path

    max_bytes = int(config.get("automation_log_max_bytes", 2097152))
    backup_count = int(config.get("automation_log_backup_count", 5))
    handler = RotatingFileHandler(str(log_path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger, log_path
