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
cd "${ROOT_DIR}" || {
  printf 'Failed to cd to repo root: %s\n' "${ROOT_DIR}" >&2
  exit 1
}

failures=0

# Rule 1 — no CRLF in shell scripts (working tree).
# Source the candidate list from `git ls-files` (matches rules 1b/2 below) so
# untracked/scratch shell files in a contributor's local tree don't trip the
# pre-push gate with false positives.
crlf_offenders="$(
  git ls-files -z "*.sh" "*.command" 2>/dev/null \
  | xargs -0 file 2>/dev/null \
  | awk -F: '/CRLF/ {print $1}'
)"
if [[ -n "${crlf_offenders}" ]]; then
  printf 'CRLF line terminators in shell scripts (must be LF):\n%s\n\n' "${crlf_offenders}" >&2
  printf 'Fix: tr -d "\\r" < <file> > <file>.tmp && mv <file>.tmp <file> && git add --renormalize <file>\n\n' >&2
  ((failures+=1))
fi

# Rule 1b — index agrees with .gitattributes eol=lf
index_eol_offenders="$(
  git ls-files --eol "*.sh" "*.command" 2>/dev/null \
  | awk '$1 != "i/lf" || $2 != "w/lf" {print}'
)"
if [[ -n "${index_eol_offenders}" ]]; then
  printf 'Files where git index/working-tree EOL is not LF (.gitattributes pins eol=lf):\n%s\n\n' "${index_eol_offenders}" >&2
  ((failures+=1))
fi

# Rule 2 — every .command / .sh is 100755 in the index
mode_offenders="$(
  git ls-files --stage "*.command" "*.sh" 2>/dev/null \
  | awk '$1 != "100755" {print $4}'
)"
if [[ -n "${mode_offenders}" ]]; then
  printf '.command / .sh files NOT marked executable in git index (need 100755):\n%s\n\n' "${mode_offenders}" >&2
  printf 'Fix: chmod +x <file> && git update-index --chmod=+x <file>\n\n' >&2
  ((failures+=1))
fi

# Rule 3 — extensionless executable scripts (scripts/git-hooks/*, etc.) ALSO
# need LF + mode 100755. Rules 1-2 glob on `.sh` / `.command` so they miss
# files with no extension — the project's git hooks live in scripts/git-hooks/
# with no suffix, are bash scripts, and are corruptible by the same Windows-
# editor CRLF + core.filemode-false combo. Gemini MED round 4 on PR #80:
# without this rule, a contributor editing the pre-commit hook on Windows
# could silently introduce CRLF (which breaks the shebang on macOS, exactly
# as for any other .sh) and the gate would pass.
#
# The candidate list is `git ls-files scripts/git-hooks/*` minus anything
# already covered above (defensive — no glob overlap today). Add new
# extensionless-script dirs here as they appear.
EXTLESS_PATHS=(
  scripts/git-hooks/pre-commit
  scripts/git-hooks/post-commit
  scripts/git-hooks/pre-push
)
extless_crlf_offenders=""
extless_mode_offenders=""
for path in "${EXTLESS_PATHS[@]}"; do
  if ! git ls-files --error-unmatch -- "${path}" >/dev/null 2>&1; then
    continue   # not tracked; skip silently
  fi
  if file "${path}" 2>/dev/null | grep -q 'CRLF'; then
    extless_crlf_offenders+="${path}"$'\n'
  fi
  if [[ "$(git ls-files --stage -- "${path}" | awk '{print $1}')" != "100755" ]]; then
    extless_mode_offenders+="${path}"$'\n'
  fi
done
if [[ -n "${extless_crlf_offenders}" ]]; then
  printf 'CRLF line terminators in extensionless executable scripts (must be LF):\n%s\n' "${extless_crlf_offenders}" >&2
  printf 'Fix: tr -d "\\r" < <file> > <file>.tmp && mv <file>.tmp <file> && git add --renormalize <file>\n\n' >&2
  ((failures+=1))
fi
if [[ -n "${extless_mode_offenders}" ]]; then
  printf 'Extensionless executable scripts NOT marked 100755 in git index:\n%s\n' "${extless_mode_offenders}" >&2
  printf 'Fix: chmod +x <file> && git update-index --chmod=+x <file>\n\n' >&2
  ((failures+=1))
fi

if [[ ${failures} -eq 0 ]]; then
  printf 'macOS portability check: PASS (no CRLF, all shell scripts are 100755 LF)\n'
  exit 0
fi

printf 'macOS portability check: FAIL (%d category/categories above)\n' "${failures}" >&2
exit 1
