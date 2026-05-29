# macOS ↔ Windows bounce traps (NON-NEGOTIABLE pre-PR check matrix)

> **Relocated from `CLAUDE.md` (2026-05-29) to reduce always-loaded context.**
> Still binding. **Run this matrix before opening any PR.** The agent-private
> version lives in the memory file `feedback_macos_windows_bounce_traps.md`.

The user works on **both macOS (primary dev) and Windows (verification + use)**.
Bugs that only one OS can trigger have caused multiple cross-OS bounces
(PR #49 → PR #50 → PR #51). This section catalogs the known traps so
agents run the pre-PR check matrix BEFORE shipping, not after the user
pulls on the other box and finds the bug.

**Run the matrix before opening any PR.** Pick the 2-3 traps that apply
to your diff and run the corresponding check on the OS you're on. If a
check requires the other OS and you can't run it, NOTE THAT explicitly
in the PR description — don't silently ship and hope.

## Trap 1: dist build bloat (gitignored ≠ excluded)

`distribution/release_prep.py` sweeps the working tree, not `git ls-files`.
Gitignored research dirs / fixture files only ship if the contributor
has them locally — which differs per OS. PR #50 missed `.venv311`
(532 MB zip). PR #51 missed `oldcam-testing/*.mp4`, `test-material/`,
`oldcam_reference_bundle/`, `analysis_frames/`, stray `*.zip` siblings,
AND a PII leak (`docs/analysis/sourav_*_results.json` shipping 78+40
SSN-format identifiers). The accumulated trash differs per OS — only
the Windows box had the .mp4 fixtures.

**Check (before any PR touching `release_prep.py`):**
```bash
"venv/Scripts/python.exe" distribution/build_release.py   # Win
.venv311/bin/python distribution/build_release.py         # macOS
ls -lah dist/SelfieGenUltimate-v*.zip   # must be <15 MB
```
Then open the zip and grep contents for PII patterns (`\d{3}-\d{2}-\d{4}`),
stray `*.zip` entries, and `tests/` leakage. The regression test
`test_copy_sanitized_tree_excludes_local_only_research_dirs` derives
expected exclusions from the `LOCAL_ONLY_RESEARCH_DIRS` + `PII_EXCLUDED_FILES`
constants — when adding a new gitignored research dir or PII-bearing
file, also add it to the corresponding constant. Anti-circularity
guard in the test asserts EXPECTED_MINIMUM ↔ constants.

## Trap 2: Launcher arg-forwarding + EOL + exec-bit asymmetry

`.bat` needs CRLF + `%*`; `.command`/`.sh` needs LF + `"$@"` + exec bit
`100755`. They're mirror constraints, but agents usually edit only one
half. PR #49 added 5 arg-forward fixes after the chain dropped
`--workspace` on its way to `gui_launcher.py`.

**Check (before any PR touching launchers):**
```bash
bash scripts/check_macos_portability.sh                # exit 0
grep -E '%\*|"\$@"' run_gui.{bat,command,sh} launchers/{macos,windows}/run_gui.*
git ls-files --eol run_gui.{bat,command,sh}            # bat=crlf, sh/command=lf
git ls-files --stage run_gui.{command,sh}              # leading 100755
```

## Trap 3: Windows-specific process behaviors (file-handle contention)

Windows Defender / Search Indexer / Explorer hold file handles during
scan. `rd /S /Q` fails with `"process cannot access the file"`. macOS
has no equivalent → never reproduces there. PR #51 H1 fix added
3-attempt retry (2s + 4s sleeps) in `launchers/windows/run_gui.bat
:release_setup_lock` precisely for this.

**Check (any new Windows .bat that does file-overwrite / dir-delete):**
add defensive retry. Test on Windows by holding a file handle inside
the target via a separate Python process — see `tests/test_launcher_arg_forwarding.py
::test_windows_release_setup_lock_retries_on_failure` for the static-text
regression guard pattern.

## Trap 4: Path-separator + case-sensitivity assertions

`os.path.join("F:\\foo", "bar.bat")` returns `"F:\\foo/bar.bat"` on
POSIX, `"F:\\foo\\bar.bat"` on Windows. Tests asserting on a literal
path string need `@pytest.mark.skipif(os.name != "nt", reason="...")`.
Already documented as macOS Portability rule 4 (see
[`docs/macos-portability.md`](macos-portability.md)); included here as a
trap-class reminder.

## Trap 5: Linter rewrites `>nul` → `/dev/null` in `.bat`

A linter in this checkout silently substitutes POSIX `/dev/null` for
Windows `>nul` in .bat files. Fires AFTER write, BEFORE stage. Three
defenses (use all):
- Existing tripwire test `test_windows_bat_has_no_dev_null` catches at
  pytest time.
- When writing `.bat` via Python, chain `python write.py && git add file.bat`
  in ONE Bash command — no window for the linter to fire.
- After staging, verify: `git show :file.bat | grep -c '/dev/null'` must be 0.

## Trap 6: gitignored file shows up in working tree on one OS only

macOS Finder creates `.DS_Store` on browse. Windows Explorer creates
`Thumbs.db` / `desktop.ini`. PR #51 round-1 found that `.DS_Store`
inside an orphan runtime dir would have permanently blocked
`workspace_markers._safe_rmtree_orphan_runtime` cleanup. Fix: include
these OS-junk names in any "safe to delete" allow-list.

## Trap 7: cmd nested-block parens crash (`. was unexpected at this time.`)

Reproduced on Windows 11 25H2 in PR #55. Inside a parenthesized `if`/`for`
block in a `.bat`, an `echo.` (dot form) or an unescaped closing paren in an
`echo`d string can crash the parser with `'. was unexpected at this time.'`
or `') was unexpected at this time.'`. The dot-echo form is the usual
culprit when `enabledelayedexpansion` is active inside a nested block.

**Check (any new nested `(...)` block in a `.bat`):** use `echo(` not
`echo.` for blank lines (see windows-launcher rule 2 in
[`docs/windows-launcher-and-sash-rules.md`](windows-launcher-and-sash-rules.md)),
and escape literal parens in echoed text as `^(` / `^)`. Prefer flattening
deeply nested blocks into `:label` subroutines (the `:check_py` pattern in
`similarity/run_gui.bat`) to dodge delayed-expansion landmines entirely.

## Pattern: when you DO catch a one-side-only bug

Add it as a new trap entry with the specific symptom + check. The list
grows; the bounce frequency drops. The memory file
`feedback_macos_windows_bounce_traps.md` has the agent-private version.
