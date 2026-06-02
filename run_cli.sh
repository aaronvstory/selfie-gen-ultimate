#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv-macos/bin/python"
REQUIREMENTS_STAMP="${ROOT_DIR}/.venv-macos/.requirements.sha256"
HEALTH_STAMP="${ROOT_DIR}/.venv-macos/.health.sha256"

# Release version banner. Parsed from app_version.py text (no Python needed,
# so it prints even on a fresh Mac before the venv exists). Single source of
# truth: app_version.RELEASE_VERSION — the same constant the GUI chip, the
# Windows launcher banner, and the release-zip name all read. `|| true`
# keeps a parse miss from tripping `set -e`.
APP_VER="$(sed -n 's/^RELEASE_VERSION[[:space:]]*=[[:space:]]*"\(.*\)".*/\1/p' \
  "${ROOT_DIR}/app_version.py" 2>/dev/null | head -1 || true)"
[[ -n "${APP_VER:-}" ]] || APP_VER="unknown"
printf '\n'
printf '  ============================================================\n'
printf '   Ultimate-Selfie-Gen  %s  --  CLI Launcher\n' "${APP_VER}"
printf '  ============================================================\n\n'

# setup_macos.sh installs deps and writes REQUIREMENTS_STAMP when they change
"${ROOT_DIR}/setup_macos.sh"
export KLING_SKIP_PY_STARTUP_DEP_CHECK=1

# Run the runtime health probe on EVERY launch (v2.17). The previous
# "skip when REQUIREMENTS_STAMP == HEALTH_STAMP" short-circuit meant that
# once the two stamps matched, a venv that broke LATER (numpy 2.x re-pulled
# by an unrelated pip run, an OS update breaking a wheel) was never
# re-detected — the macOS twin of the run_cli.bat unconditional-stamp bug.
# The probe is ~3-5s; matching run_gui.sh which already probes every launch.
# Clear HEALTH_STAMP before repair so a failed repair can't leave a stale
# "healthy" marker.
if [[ -f "${ROOT_DIR}/dependency_health_check.py" ]]; then
  # Refresh HEALTH_STAMP ONLY on a successful probe / successful repair
  # (matches run_gui.sh). Writing it unconditionally would cache a venv whose
  # repair FAILED as "healthy", so the next launch skips the probe and the
  # user is stuck with a broken venv (code-review C1, 2026-06-03).
  if "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode check; then
    cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
  else
    echo "Runtime dependency health check failed. Attempting auto-repair..." >&2
    rm -f "${HEALTH_STAMP}" 2>/dev/null || true
    if "${PYTHON_BIN}" "${ROOT_DIR}/dependency_health_check.py" --mode repair; then
      cp "${REQUIREMENTS_STAMP}" "${HEALTH_STAMP}" 2>/dev/null || true
    else
      # ABORT the CLI launch on repair failure, matching run_gui.sh (code-review
      # Codex P2): proceeding would drop the user into the CLI menu where any
      # face feature (crop / similarity / automation) then fails confusingly.
      # HEALTH_STAMP stays cleared so the next launch re-probes + re-repairs.
      echo "" >&2
      echo "============================================================" >&2
      echo "ERROR: Automatic dependency repair FAILED." >&2
      echo "============================================================" >&2
      echo "The runtime health probe failed AND auto-repair did not fix it." >&2
      echo "Recover manually, then re-run:" >&2
      echo "  1. Delete the venv + re-bootstrap:" >&2
      echo "       rm -rf \"${ROOT_DIR}/.venv-macos\" && bash \"${ROOT_DIR}/run_cli.sh\"" >&2
      echo "  2. Or force-reinstall the face stack (mirrors REPAIR_PACKAGES):" >&2
      echo "       \"${PYTHON_BIN}\" -m pip install --force-reinstall --no-cache-dir \\" >&2
      echo "         numpy==1.26.4 tensorflow==2.16.2 protobuf==4.25.3 tf-keras==2.16.0 \\" >&2
      # Single-quote scipy/absl specs: > and < are bash redirection if the user
      # copy-pastes the printed command unquoted (code-review HIGH 2026-06-03).
      echo "         retina-face==0.0.17 deepface==0.0.92 'scipy>=1.11,<2' 'absl-py>=2.3,<3'" >&2
      exit 1
    fi
  fi
fi

# Auto-detect NVIDIA + bootstrap CuPy (same as run_gui.sh — see that
# launcher for the full design rationale + opt-out env var).
if [[ -f "${ROOT_DIR}/scripts/gpu_bootstrap.py" ]]; then
  "${PYTHON_BIN}" "${ROOT_DIR}/scripts/gpu_bootstrap.py" --quiet-if-cached || true
fi

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/kling_automation_ui.py"
