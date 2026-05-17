#!/usr/bin/env bash
set -euo pipefail

# oldcam-testing A/B harness launcher (macOS).
# Mirrors run_ab_test.bat: resolves a supported python (3.9 <= ver < 3.13),
# preferring the shared repo venv, then runs run_ab_test.py with all args.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

_python_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 13) else 2)' >/dev/null 2>&1
}

resolve_py() {
  if [ -n "${SELFIEGEN_PYTHON:-}" ] && [ -x "${SELFIEGEN_PYTHON}" ] && _python_supported "${SELFIEGEN_PYTHON}"; then
    echo "${SELFIEGEN_PYTHON}"; return 0
  fi
  for candidate in \
    "$REPO_ROOT/.venv-macos/bin/python" \
    "$REPO_ROOT/.venv311/bin/python" \
    "$REPO_ROOT/venv/bin/python" \
    "$REPO_ROOT/.venv/bin/python"; do
    if [ -x "$candidate" ] && _python_supported "$candidate"; then
      echo "$candidate"; return 0
    fi
  done
  # PATH probe order must cover the full _python_supported range
  # (3.9 <= ver < 3.13). 3.11 leads per CLAUDE.md Rule 6 (Tk on Homebrew),
  # then descending. macOS setups that only ship python3.10 or python3.9
  # (no python3 shim) hit the versioned probes — fixed per Codex review.
  for bin in python3.11 python3.12 python3.10 python3.9 python3 python; do
    if command -v "$bin" >/dev/null 2>&1 && _python_supported "$(command -v "$bin")"; then
      command -v "$bin"; return 0
    fi
  done
  return 1
}

PYBIN="$(resolve_py || true)"
if [ -z "$PYBIN" ]; then
  echo "[FATAL] No supported Python (3.9 <= ver < 3.13) found." >&2
  echo "        Tried: SELFIEGEN_PYTHON, $REPO_ROOT/.venv-macos, .venv311, venv, .venv," >&2
  echo "        then python3.11/3.12/3 on PATH." >&2
  exit 127
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] oldcam-testing A/B run"
echo "Repo root: $REPO_ROOT"
echo "Python:    $PYBIN"

if [ "$#" -eq 0 ]; then
  echo
  echo "Usage: run_ab_test.command \"/path/to/kling_clip.mp4\" [more.mp4 ...]"
  echo "       run_ab_test.command \"clip.mp4\" --no-score"
  echo "       run_ab_test.command --version v25 --source \"/path/to/source.mp4\""
  exit 2
fi

exec "$PYBIN" "$SCRIPT_DIR/run_ab_test.py" "$@"
