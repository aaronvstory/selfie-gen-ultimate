#!/usr/bin/env bash
# ============================================================
#  Ultimate-Selfie-Gen  --  Automation BATCH launcher (headless, macOS)
# ------------------------------------------------------------
#  Thin wrapper: delegates to the canonical CLI launcher chain
#  (launchers/run_cli.command -> launchers/macos/run_cli.command ->
#  run_cli.sh) so the FULL v2.17 dependency bootstrap runs --
#  setup_macos.sh deps, GPU/MPS CuPy bootstrap, health probe + repair --
#  then launches the automation pipeline NON-INTERACTIVELY via
#  "kling_automation_ui.py --batch".
#
#  Usage (double-click, or from a terminal / cron):
#    ./run_auto.command "/path/to/cases_root" [--limit N] [--reprocess MODE]
#
#  Exit code is the pipeline status (0=success, non-zero=failure) so
#  launchd / cron can detect failed jobs.
# ============================================================
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${ROOT_DIR}/launchers/run_cli.command"

if [[ ! -f "${TARGET}" ]]; then
  printf 'Missing launcher: %s\n' "${TARGET}" >&2
  read -r -p "Press Enter to close..."
  exit 1
fi

if [[ ! -x "${TARGET}" ]]; then
  chmod +x "${TARGET}" || true
fi

# Inject --batch and forward every user arg (root folder, --limit, etc).
exec "${TARGET}" --batch "$@"
