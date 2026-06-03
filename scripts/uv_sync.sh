#!/usr/bin/env bash
# ===========================================================================
#  uv_sync.sh  (v2.20)
#  Shared uv fast-path for the macOS / Linux launchers. SOURCED (not executed)
#  so it can set UV_SYNCED in the caller's shell, mirroring the Windows
#  scripts/win_uv_sync.bat helper.
#
#  Attempts the uv-native dependency sync (scripts/uv_sync_deps.py: ensure uv
#  -> GPU-aware torch extra -> uv sync) and reports the outcome so the caller
#  can skip its legacy setup_macos.sh + pip block.
#
#  Usage (sourced):
#      UV_SYNCED=""
#      selfiegen_uv_sync "<python_bin>" "<ROOT_DIR>"
#      if [[ -n "${UV_SYNCED}" ]]; then exec <app>; fi
#      # else fall through to setup_macos.sh
#
#  Args:  $1 = python interpreter to RUN the orchestrator (uv provisions the
#              project env itself at ROOT/.venv-macos)
#         $2 = ROOT_DIR (repo root holding uv.lock + scripts/)
#
#  Sets:  UV_SYNCED=1   when the uv path produced a ready env.
#         UV_SYNCED=""   when the caller must FALL BACK to the pip path.
#
#  Opt-out: KLING_USE_PIP=1 forces the legacy pip path. Best-effort: any
#  failure leaves UV_SYNCED empty so the caller's pip path takes over.
# ===========================================================================

selfiegen_uv_sync() {
  UV_SYNCED=""
  local _uv_py="${1:-}"
  local _uv_root="${2:-}"
  if [[ "${KLING_USE_PIP:-}" == "1" ]]; then return 0; fi
  if [[ -z "${_uv_py}" || -z "${_uv_root}" ]]; then return 0; fi
  if [[ ! -f "${_uv_root}/uv.lock" ]]; then return 0; fi
  if [[ ! -f "${_uv_root}/scripts/uv_sync_deps.py" ]]; then return 0; fi
  if [[ ! -x "${_uv_py}" ]]; then
    # The orchestrator can also run under any python3 on PATH (it's stdlib
    # only); uv itself provisions the real env. Fall back to PATH python3.
    if command -v python3 >/dev/null 2>&1; then
      _uv_py="$(command -v python3)"
    else
      return 0
    fi
  fi
  printf '  [uv] syncing dependencies via uv (set KLING_USE_PIP=1 to force pip)...\n'
  # uv_sync_deps exit codes: 0 = env ready; 3 = fall back to pip.
  if "${_uv_py}" "${_uv_root}/scripts/uv_sync_deps.py" --project "${_uv_root}"; then
    UV_SYNCED=1
    printf '  [uv] dependencies ready (uv-managed venv).\n'
  fi
  return 0
}
