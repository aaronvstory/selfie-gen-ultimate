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
#  Usage (from a terminal / cron / launchd -- NOT double-click: --batch needs a
#  ROOT path argument, which Finder double-click cannot supply):
#    ./run_auto.command "/path/to/cases_root" [--limit N] [--reprocess MODE]
#
#  Exit codes (pipeline status, so launchd / cron can detect failures):
#    0 = ran, every case clean
#    1 = could not run (missing root / no cases / preflight fail / exception)
#    2 = ran, but one or more cases ended failed or manual_review
# ============================================================
set -uo pipefail

# Headless: tell the canonical launcher chain to NOT pause on failure (a
# read/keypress wait would hang an unattended cron / launchd job forever).
# Every pause/read in the chain is guarded behind this var.
export KLING_NONINTERACTIVE=1

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="${ROOT_DIR}/launchers/run_cli.command"

if [[ ! -f "${TARGET}" ]]; then
  printf 'Missing launcher: %s\n' "${TARGET}" >&2
  exit 1
fi

if [[ ! -x "${TARGET}" ]]; then
  chmod +x "${TARGET}" || true
fi

# Inject --batch and forward every user arg (root folder, --limit, etc).
exec "${TARGET}" --batch "$@"
