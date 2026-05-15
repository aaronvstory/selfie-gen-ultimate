#!/usr/bin/env bash
# Pre-push macOS portability check.
#
# Catches the two recurring macOS-runtime regressions that bite when this
# repo is edited from Windows:
#   1. CRLF line endings in *.sh / *.command (shebang resolves to "bash\r")
#   2. *.command / *.sh committed with mode 100644 (Finder cannot launch)
#
# Run from the repo root:
#   bash scripts/check_macos_portability.sh
# Exits non-zero if any offender is found.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

failures=0

# Rule 1 — no CRLF in shell scripts (working tree)
crlf_offenders="$(
  find . \( -name "*.sh" -o -name "*.command" \) \
    -not -path "./.venv*" -not -path "./.git/*" \
    -exec file {} + 2>/dev/null \
  | awk -F: '/CRLF/ {print $1}'
)"
if [[ -n "${crlf_offenders}" ]]; then
  printf 'CRLF line terminators in shell scripts (must be LF):\n%s\n\n' "${crlf_offenders}" >&2
  printf 'Fix: tr -d "\\r" < <file> > <file>.tmp && mv <file>.tmp <file> && git add --renormalize <file>\n\n' >&2
  failures=1
fi

# Rule 1b — index agrees with .gitattributes eol=lf
index_eol_offenders="$(
  git ls-files --eol "*.sh" "*.command" 2>/dev/null \
  | awk '$1 != "i/lf" || $2 != "w/lf" {print}'
)"
if [[ -n "${index_eol_offenders}" ]]; then
  printf 'Files where git index/working-tree EOL is not LF (.gitattributes pins eol=lf):\n%s\n\n' "${index_eol_offenders}" >&2
  failures=1
fi

# Rule 2 — every .command / .sh is 100755 in the index
mode_offenders="$(
  git ls-files --stage "*.command" "*.sh" 2>/dev/null \
  | awk '$1 != "100755" {print $4}'
)"
if [[ -n "${mode_offenders}" ]]; then
  printf '.command / .sh files NOT marked executable in git index (need 100755):\n%s\n\n' "${mode_offenders}" >&2
  printf 'Fix: chmod +x <file> && git update-index --chmod=+x <file>\n\n' >&2
  failures=1
fi

if [[ ${failures} -eq 0 ]]; then
  printf 'macOS portability check: PASS (no CRLF, all .command/.sh are 100755 LF)\n'
  exit 0
fi

printf 'macOS portability check: FAIL (%d category/categories above)\n' "${failures}" >&2
exit 1
