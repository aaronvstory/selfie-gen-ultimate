#!/usr/bin/env bash

cd "$(dirname "$0")" || exit 1

LOG_FILE="$(pwd)/launcher_runtime.log"
{
  echo
  echo "==============================================================================="
  echo "[INFO] [$(date '+%Y-%m-%d %H:%M:%S')] Starting run_cli.command in $(pwd)"
} >> "${LOG_FILE}"
if [ -n "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
    exec >> "${LOG_FILE}" 2>&1
else
    exec > >(tee -a "${LOG_FILE}") 2>&1
fi

export TF_USE_LEGACY_KERAS=1
export KERAS_BACKEND=tensorflow
export PYTHONNOUSERSITE=1
unset PYTHONPATH
unset PYTHONHOME

sanitize_known_bad_pth() {
    candidate="$1"
    if [ -z "$candidate" ]; then
        return 0
    fi
    "$candidate" - <<'PY'
import site
from pathlib import Path

targets = []
for root in (site.getsitepackages() or []):
    targets.append(Path(root) / "protobuf-3.19.6-nspkg.pth")
try:
    user_site = site.getusersitepackages()
except Exception:
    user_site = None
if user_site:
    targets.append(Path(user_site) / "protobuf-3.19.6-nspkg.pth")

for path in targets:
    if path.exists():
        try:
            path.unlink()
            print(f"[INFO] Removed incompatible site-packages artifact: {path}")
        except Exception:
            pass
PY
}

pick_supported_python() {
    for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
        if ! command -v "$candidate" >/dev/null 2>&1; then
            continue
        fi
        if "$candidate" -c 'import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)' >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON_BIN="$(pick_supported_python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] No supported Python found (requires 3.9-3.12 for TensorFlow/DeepFace)."
    echo "Install Python 3.12 and retry (macOS: brew install python@3.12)."
    if [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
        read -r -p "Press Enter to exit..."
    fi
    exit 1
fi

echo "[INFO] Using Python interpreter: $PYTHON_BIN"
sanitize_known_bad_pth "$PYTHON_BIN"

if [ ! -f ".venv/bin/activate" ]; then
    echo "[INFO] Virtual environment not found. Creating one..."
    sanitize_known_bad_pth "$PYTHON_BIN"
    "$PYTHON_BIN" -m venv .venv
    if [ $? -ne 0 ]; then
        echo "[ERROR] Failed to create virtual environment."
        if [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
            read -r -p "Press Enter to exit..."
        fi
        exit 1
    fi
else
    if ! .venv/bin/python -c 'import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)' >/dev/null 2>&1; then
        echo "[INFO] Existing virtual environment uses unsupported Python. Recreating..."
        rm -rf .venv
        sanitize_known_bad_pth "$PYTHON_BIN"
        "$PYTHON_BIN" -m venv .venv
        if [ $? -ne 0 ]; then
            echo "[ERROR] Failed to recreate virtual environment."
            if [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
                read -r -p "Press Enter to exit..."
            fi
            exit 1
        fi
    fi
    echo "[INFO] Activating existing virtual environment..."
fi

echo "[INFO] Activating virtual environment..."
source .venv/bin/activate
if [ $? -ne 0 ]; then
    echo "[ERROR] Failed to activate virtual environment."
    if [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
        read -r -p "Press Enter to exit..."
    fi
    exit 1
fi

echo "[INFO] Synchronizing dependencies from requirements.txt..."
python -m pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo "[ERROR] Failed to synchronize dependencies from requirements.txt."
    if [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
        read -r -p "Press Enter to exit..."
    fi
    exit 1
fi

echo "[INFO] Launching Face Similarity CLI..."
python main.py --cli
EXIT_CODE=$?

echo "[INFO] Application finished with code $EXIT_CODE"
if [ -z "${SIMILARITY_LAUNCHED_BY_MAIN:-}" ]; then
    read -r -p "Press Enter to exit..."
fi

exit $EXIT_CODE
