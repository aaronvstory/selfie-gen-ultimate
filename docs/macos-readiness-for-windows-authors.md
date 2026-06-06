# macOS readiness — proactive checklist for Windows-side authors

> Most commits to this repo are authored on Windows. The macOS round-trip
> ("does it run on the Mac box?") is verified later by a contributor who
> can't easily go back and re-do invisible breakage. This doc is the **5-
> minute checklist** that prevents the bounce-back loop. Run the relevant
> rows BEFORE pushing, not after the macOS dev pulls and finds the bug.
>
> Pairs with [`macos-portability.md`](macos-portability.md) (the
> 14 binding rules the runtime enforces) and
> [`cross-os-bounce-traps.md`](cross-os-bounce-traps.md) (the 7-trap
> pre-PR matrix). This doc is the **author-side** complement — what the
> Windows author should think about while writing the diff, not just
> what to check after.

---

## Why this exists

The v2.21 → v2.24 macOS sync round (PR #79) caught 3 darwin-specific
regressions the Windows author couldn't see locally:

1. **HIGH** — `tkinterdnd2 0.4.4` bundles a Tcl 9.x `osx-arm64` binary;
   macOS python.org Python 3.11 ships Tcl 8.6.12 → stubs mismatch → DnD
   silently dies on every Apple Silicon launch. The PR #61 graceful-fail
   layer swallows the error so the user sees only an obscure log line.
   Invisible from Windows where the bundled binary is win-x64 (different
   wheel).
2. **MED** — `pytest` was undeclared in `pyproject.toml` / `requirements.txt`
   / `uv.lock`. Every `uv sync` on macOS actively UNINSTALLED pytest,
   breaking the CLAUDE.md pre-commit invariant `pytest tests/ -q`. On
   Windows the author had pytest ambient via system Python so the gap
   was invisible.
3. **LOW** — 2 `test_gpu_bootstrap.py` tests assumed Windows CUDA semantics
   (monkeypatched `detect_nvidia` to return a CUDA dict) and asserted the
   stamp token differs. On darwin `resolve_torch_mode` hard-returns
   `mac_default` regardless of nvidia state — the tests false-failed.

Pattern: **everything the Windows author did was internally correct, but
macOS-specific surfaces had no automated guard.** This checklist is how
to write those guards proactively.

---

## When your diff touches each of these, do this BEFORE pushing

### 1. Bumping a Tk-related Python dep (tkinterdnd2, customtkinter, ttkbootstrap, tksvg, …)

Native Tk libraries are bundled INSIDE these wheels as per-platform
binaries. A safe-looking version bump can change the bundled `osx-arm64/`
binary's Tcl ABI. The python.org macOS Python 3.11 we ship against uses
Tcl 8.6.12 — a wheel bundling Tcl 9.x silently breaks Apple Silicon.

**Check before pushing:**

```powershell
# Windows PowerShell — download the new wheel and inspect its osx-arm64 contents.
# PowerShell 5.1's Expand-Archive only accepts `.zip`, so we rename the .whl
# first. Adjust $TEMP_DIR to wherever you scratch files (default C:\temp\check).
$pkg = "tkinterdnd2"
$ver = "0.4.4.1"          # the version you're bumping TO
$TEMP_DIR = "C:\temp\check"
New-Item -ItemType Directory -Force -Path $TEMP_DIR | Out-Null
pip download --no-deps "$pkg==$ver" -d $TEMP_DIR
Get-ChildItem $TEMP_DIR\*.whl | ForEach-Object {
    Copy-Item $_.FullName ($_.FullName -replace '\.whl$', '.zip')
}
Expand-Archive $TEMP_DIR\*.zip -DestinationPath $TEMP_DIR\extracted -Force
Get-ChildItem -Recurse $TEMP_DIR\extracted -Filter "*.dylib" | Select FullName, Length
# If the dylib filename starts with libtcl9... → Tcl 9.x → BREAKS macOS Tk 8.6.
# Cap to the last known Tcl-8.6-bundled version (for tkinterdnd2, that's <0.4.4).
```

> **bash / zsh equivalent** (macOS or git-bash on Windows):
> ```bash
> pkg=tkinterdnd2 ; ver=0.4.4.1
> pip download --no-deps "$pkg==$ver" -d /tmp/check
> unzip -q /tmp/check/${pkg}-${ver}-*.whl -d /tmp/check/extracted
> ls /tmp/check/extracted/${pkg}/tkdnd/osx-arm64/
> file /tmp/check/extracted/${pkg}/tkdnd/osx-arm64/*.dylib
> ```

**Add a regression test** for any new cap, mirroring
`tests/test_macos_tkdnd_loads.py`:

- Source guard: every dep-declaration site must include the cap (root
  `requirements.txt`, root `pyproject.toml`, `distribution/pyproject.toml`,
  `dependency_checker.py` `pip_name=`, sub-project `requirements.txt`s).
- `uv.lock` guard: parse via `tomllib` and assert the resolved version
  respects the cap.
- Darwin-arm64-gated real-import probe: actually instantiate the runtime
  symbol (e.g. `tkinterdnd2.TkinterDnD.Tk()`) and assert it doesn't
  raise `RuntimeError: Unable to load tkdnd library`.

### 2. Adding a test that mocks platform / CUDA / nvidia state

If your test monkeypatches `detect_nvidia`, `cuda_major`, `nvidia-smi`,
or otherwise simulates Windows-CUDA semantics: **pin `sys.platform`
inside the test**.

```python
# WRONG — passes on Windows where sys.platform == "win32", false-fails on darwin
def test_compute_stamp_token_changes_when_cuda_appears(monkeypatch):
    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia", lambda: {"cuda_major": 12})
    assert "cuda" in gpu_bootstrap.compute_stamp_token()
    # ↑ on darwin this is False — resolve_torch_mode short-circuits to mac_default

# RIGHT — Windows-semantics invariant runs portably
def test_compute_stamp_token_changes_when_cuda_appears(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")        # dotted-path form
    monkeypatch.setattr(gpu_bootstrap, "detect_nvidia", lambda: {"cuda_major": 12})
    assert "cuda" in gpu_bootstrap.compute_stamp_token()
```

Use the **dotted-path** `monkeypatch.setattr("sys.platform", "win32")`,
not `monkeypatch.setattr(some_module.sys, "platform", "win32")`. The
dotted form is decoupled from how the module-under-test imported sys;
the attribute form silently misses or AttributeErrors if the module
ever switches to `from sys import platform`.

### 3. Adding any dep your test suite needs

CLAUDE.md's pre-commit invariant is `pytest tests/ similarity/tests/ -q`.
If you add a test that requires a new dep (pytest-asyncio, hypothesis,
freezegun, …), put it in **`[project.optional-dependencies].dev`** in
`pyproject.toml` AND in `distribution/pyproject.toml`. (The `dev` extra
itself landed in
[PR #79](https://github.com/aaronvstory/selfie-gen-ultimate/pull/79)
during the v2.24 round.) Then re-resolve the lock:

```bash
uv lock
```

End-user launchers DO NOT install the `dev` extra (they run
`uv sync --no-default-groups --extra <cpu/cu*>`), so the dep stays out
of deployed envs — contributors opt-in via `uv sync --extra cpu --extra dev`
or `uv sync --extra cu128 --extra dev`. See the comment on the `dev`
extra in `pyproject.toml` for the full rationale.

### 4. Touching `dependency_checker.py` or `distribution/dependency_checker.py`

These are the documented `python dependency_checker.py` repair-path
install sites. **Every `pip_name=` must carry the full spec from
requirements.txt**, never a bare package name:

```python
# WRONG — bypasses every cap, pip installs latest
Dependency(name="TkinterDnD2", import_name="tkinterdnd2", pip_name="tkinterdnd2", ...)

# RIGHT — mirrors the cap in requirements.txt
Dependency(name="TkinterDnD2", import_name="tkinterdnd2", pip_name="tkinterdnd2<0.4.4", ...)
```

Both copies (root + `distribution/`) must agree. PR #79 had to fix both
copies separately — first the root in commit `23433031` (round 2),
then the dist mirror in `b941d2a6` (round 4) after Codex caught the
gap. Fix both at once in your branch to avoid that bounce.

### 5. Adding a new `pip install` site

CLAUDE.md's "numpy<2 / constraints.txt invariant" is non-negotiable.
Every `pip install` call site (sub-launcher, repair path, health
check, `setup_macos.sh`, oldcam version launchers) must thread
`-c constraints.txt` so transitive resolves can't float numpy past
1.x and break the TF 2.16.2 face stack.

On macOS the `setup_macos.sh` pattern is:

```bash
CONSTRAINTS_FILE="${ROOT_DIR}/constraints.txt"
CONSTRAINTS_ARG=()
if [ -f "${CONSTRAINTS_FILE}" ]; then
  CONSTRAINTS_ARG=(-c "${CONSTRAINTS_FILE}")
fi
"${VENV_DIR}/bin/python" -m pip install --disable-pip-version-check \
  "${CONSTRAINTS_ARG[@]+"${CONSTRAINTS_ARG[@]}"}" -r "${REQUIREMENTS_FILE}"
```

The `${CONSTRAINTS_ARG[@]+"…"}` expansion is the bash-3.2-safe form
for "expand only if the array is set", which macOS Bash 3.2 + `set -u`
requires. Test on macOS Bash 3.2 (the system bash), not on macOS Bash
5 (Homebrew).

### 6. Editing a `*.sh` or `*.command` file

Three traps:

- **EOL must be LF.** Windows editors auto-convert. After every edit:
  ```bash
  git ls-files --eol <file>   # both columns must show "lf"
  ```
- **Mode must be 100755** (not 100644) so Finder can double-click
  `.command` files:
  ```bash
  git ls-files --stage <file>   # leading number must be 100755
  ```
- **The `Write` / `Edit` tools are safe for `.sh` / `.command`** (they
  emit LF, which matches what `.gitattributes` pins for these
  extensions). Do NOT use them for `.bat` / `.cmd` — those need CRLF
  and the tools emit LF, corrupting the file. For `.bat` / `.cmd`,
  write via PowerShell `WriteAllText` with explicit `` `r`n ``.

After ANY edit:

```bash
bash scripts/check_macos_portability.sh   # exit 0 required
```

This is now run automatically by the pre-commit hook (see
`scripts/git-hooks/pre-commit`), so a CRLF or exec-bit slip will
block your commit locally.

### 7. Touching the GUI carousel / sash layout / Tk geometry code

macOS Tk's Aqua port differs from Windows Tk in:

- Hit-target sizing (macOS needs more padding on small buttons).
- Sash interactive behavior (Aqua native PanedWindow vs Windows).
- Window-manager geometry events (some events arrive in different order).

If your diff touches `kling_gui/layout_utils.py`, `main_window.py`
sash-restore code, or `tabs/*.py` widget geometry — boot the GUI on
your Windows box AND ask a macOS reviewer to boot on theirs BEFORE
merging. Tk-geometry bugs don't show up in pytest; they show up as
"the button text is clipped on macOS at 1280×800."

---

## What to add to the PR description

When your diff touches any of these surfaces, include a
**"macOS readiness"** section in the PR description:

```markdown
## macOS readiness

- [ ] Tk dep bump check (osx-arm64 binary Tcl version) — N/A or `<inspection result>`
- [ ] Platform-mock tests pin `sys.platform` — N/A or `<test names>`
- [ ] New test deps in `pyproject` dev extra — N/A or `<dep name>`
- [ ] `dependency_checker.py` `pip_name=` carries spec — N/A or `<diff line>`
- [ ] New `pip install` sites thread `-c constraints.txt` — N/A or `<call site>`
- [ ] `.sh` / `.command` EOL = LF + mode = 100755 — verified via portability gate
- [ ] GUI geometry sanity on Win + Mac — N/A or `<screenshot/note>`
```

A "no" on any line is fine — better to surface "I couldn't verify on
macOS" than to silently ship and bounce.

---

## When the macOS dev still finds a regression

It happens — this checklist isn't exhaustive. The recovery loop:

1. macOS dev opens a polish PR off main (e.g.
   `feat/macos-polish-post-vX.Y`). NEVER works on main.
2. Adds a TDD regression test (real-import / real-source probe — NOT
   text grep). The test should fail BEFORE the fix.
3. Fixes the smallest unit possible. Commit + push.
4. Triggers bots + spawns a code-reviewer subagent in parallel on
   the full branch diff (see `docs/pr-review-loop.md`).
5. Polish PR merges independently of any Windows-side work.

The author-side prevention via this checklist + the runtime guard via
the new regression test together prevent the next round-trip.

---

## Related docs

- [`macos-portability.md`](macos-portability.md) — 14 binding
  runtime rules
- [`cross-os-bounce-traps.md`](cross-os-bounce-traps.md) — 7-trap
  pre-PR matrix
- [`windows-launcher-and-sash-rules.md`](windows-launcher-and-sash-rules.md)
  — Windows-side mirror of this doc
- [`uv-migration.md`](uv-migration.md) — dev extra contract
- [`pr-review-loop.md`](pr-review-loop.md) — the autonomous bot +
  subagent workflow this branch should run through
