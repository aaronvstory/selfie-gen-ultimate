# SSD + Distributables Playbook

## What this is

The user runs a **portable plug-and-play copy of selfie-gen-ultimate from
an external SSD** so they can launch the GUI on a virgin Mac without
re-installing anything from scratch. The SSD is an ExFAT volume mounted
at `/Volumes/st7Private/code/selfie-gen-ultimate/` when plugged in.

**Path conventions in this doc**: `$REPO_ROOT` refers to your local
checkout of `selfie-gen-ultimate` (commonly `~/code/selfie-gen-ultimate`);
`/Volumes/st7Private/...` is the canonical SSD mountpoint when the
external drive is plugged in. Other paths are literal.

**Two-venv discipline**: the project uses two distinct venvs on the source
Mac and the playbook calls them out separately on purpose — don't try to
"normalize" them to one. `.venv311/` is the build-tooling venv that runs
the test suite and `distribution/build_release.py`; `.venv-macos/` is the
GUI runtime venv that gets tarballed into `_user_state/venv-macos.tar`
for the SSD bootstrap. Same Python (3.11) but different package sets.

It contains:

- **Source tree** at the project root (git clone of `main`, with
  `origin` pointed at `https://github.com/aaronvstory/selfie-gen-ultimate.git`)
- **One-click launcher** at the root: `START.command` (untracked,
  in the SSD repo's `.git/info/exclude`)
- **Bootstrap kit** under `_user_state/`:
  - `install_to_appsupport.command` — seeds `~/Library/Application Support/selfie-gen-ultimate/`
  - `venv-macos.tar` — pre-built Python venv tarball (~1.9 GB) used as a
    first-launch shortcut. `setup_macos.sh` extracts + reuses if the
    target Mac's Python matches, otherwise rebuilds.
  - `app_support/` — snapshot of the source Mac's
    `~/Library/Application Support/selfie-gen-ultimate/` (API keys,
    prompts, history, model cache, sessions)
  - `README.txt` — first-run instructions for someone holding the SSD

The SSD also gets `dist/SelfieGenUltimate-{vX.Y}.zip` + alias dropped at
its project root after each merge, for shipping the bundle to others.

## When to refresh the SSD

**Whenever a PR is merged to `main` AND the SSD is mounted.** If the SSD
is unplugged at merge time, tell the user "I'd refresh the SSD but it's
not mounted — your SSD copy is now out of date by N commits." Don't
silently skip.

Detection: `ls /Volumes/st7Private 2>/dev/null && echo MOUNTED`.

## The post-merge refresh sequence (5 steps)

After `gh pr merge → git checkout main && git pull origin main`:

### Step 1: Build the distributable

```bash
cd "$REPO_ROOT"   # your local checkout, e.g. ~/code/selfie-gen-ultimate
.venv311/bin/python distribution/build_release.py
```

Reads version from `app_version.py:RELEASE_VERSION`. Writes:
- `dist/SelfieGenUltimate-{vX.Y}.zip` — versioned
- `dist/SelfieGenUltimate.zip` — alias to latest

Excluded by `distribution/release_prep.py:EXCLUDED_DIRS/EXCLUDED_FILES`:
all `.venv*` variants (including `.venv311`), `tests/`, `dist/`, `build/`,
agent state dirs (`.claude/`, `.serena/`, …), private user files
(`kling_config.json`, `kling_history.json`, `*.log`). The config that
DOES ship in the bundle is built from `default_config_template.json`
overlaid with API-keys-blanked structure from the live `kling_config.json`.

Also excluded (PR #51, Windows-side discovery): local-only research dirs
(`oldcam_reference_bundle/`, `analysis_frames/`, `test-material/`,
`oldcam-testing/rppg_harness_out/`), stray `*.zip` siblings, and
**PII-bearing corpus measurement outputs**
(`docs/analysis/sourav_*_results.json` — these contain SSN-format
persona identifiers). The build script sweeps the working tree, NOT
`git ls-files`, so `.gitignore` alone does NOT shield a dir or file
from packaging — every gitignored entry must ALSO be in
`EXCLUDED_DIRS` / `EXCLUDED_FILES` / `LOCAL_ONLY_RESEARCH_DIRS` /
`PII_EXCLUDED_FILES`. **When adding a new gitignored research dir or
PII-bearing file, also add it to the corresponding constant in
`release_prep.py`.** The regression test
`test_copy_sanitized_tree_excludes_local_only_research_dirs` derives
the expected sets from those constants.

**Expected bundle size: ~10 MB.** If it balloons to hundreds of MB,
something escaped the exclusion list (this happened twice: PR #50 fixed
the `.venv311` miss after a 532 MB zip; PR #51 fixed four more research
dirs + PII files + stray zips after a 182 MB zip).

### Step 2: Pull main onto the SSD

```bash
cd /Volumes/st7Private/code/selfie-gen-ultimate
git pull origin main
```

The SSD repo's `_user_state/` won't be touched (it's in `.git/info/exclude`
on that repo). The `M build_gui_exe.bat` "dirty" status on the SSD is a
known harmless pre-existing artifact of mixed EOL in the committed blob —
ignore it.

### Step 3: Refresh the app_support snapshot

```bash
mkdir -p /Volumes/st7Private/code/selfie-gen-ultimate/_user_state/app_support
rsync -a \
  ~/Library/Application\ Support/selfie-gen-ultimate/kling_config.json \
  ~/Library/Application\ Support/selfie-gen-ultimate/ui_config.json \
  ~/Library/Application\ Support/selfie-gen-ultimate/kling_history.json \
  ~/Library/Application\ Support/selfie-gen-ultimate/pricing_cache.json \
  ~/Library/Application\ Support/selfie-gen-ultimate/model_cache \
  ~/Library/Application\ Support/selfie-gen-ultimate/sessions \
  /Volumes/st7Private/code/selfie-gen-ultimate/_user_state/app_support/
```

The `mkdir -p` is a no-op when the dest already exists (refresh case) but
prevents a "No such file or directory" failure on a first-time SSD setup
where the bootstrap kit hasn't been laid down yet.

This keeps the SSD's snapshot of API keys, UI layout, prompt slots, and
model cache in lockstep with the live source Mac.

### Step 4: Copy the dist zips to SSD root

```bash
cp dist/SelfieGenUltimate-*.zip /Volumes/st7Private/code/selfie-gen-ultimate/
```

Drops the versioned + alias at the SSD project root for easy retrieval
when shipping the bundle out.

### Step 5: Verify

```bash
git -C /Volumes/st7Private/code/selfie-gen-ultimate log --oneline -3
ls -la /Volumes/st7Private/code/selfie-gen-ultimate/START.command \
       /Volumes/st7Private/code/selfie-gen-ultimate/_user_state/venv-macos.tar \
       /Volumes/st7Private/code/selfie-gen-ultimate/_user_state/install_to_appsupport.command \
       /Volumes/st7Private/code/selfie-gen-ultimate/_user_state/app_support/kling_config.json \
       /Volumes/st7Private/code/selfie-gen-ultimate/SelfieGenUltimate-*.zip
du -h dist/SelfieGenUltimate-*.zip      # sanity-check size ~10MB; hundreds of MB = venv leaked into bundle
```

Bootstrap chain must remain intact and the new dist zip must be at root.

## When (and only when) to rebuild the venv tarball

Skip the rebuild unless **`requirements.txt` or `requirements-hashed.txt`
changed in the merged PR.** Most PRs are pure-Python with no new deps,
so the cached venv tarball stays valid.

If they did change:

```bash
# Use the source Mac's freshly-updated .venv-macos as truth
cd "$REPO_ROOT"   # your local checkout, e.g. ~/code/selfie-gen-ultimate
.venv-macos/bin/pip install -r requirements.txt   # refresh local venv
tar -cf /Volumes/st7Private/code/selfie-gen-ultimate/_user_state/venv-macos.tar \
  --exclude '__pycache__' .venv-macos
```

Excluding `__pycache__` shaves ~330 MB off the tarball; Python regenerates
on first import.

## Troubleshooting

### SSD not visible in Finder after replug

ExFAT has no journal — uncleanly-ejected volumes get a "dirty" bit that
blocks auto-mount. The disk shows up in `diskutil list external` but
doesn't appear under `/Volumes/`. Fix:

```bash
diskutil list external | grep st7Private   # find the identifier (e.g. disk6s3)
diskutil mount disk6s3                     # mount manually
```

Prevention: always **eject cleanly** before unplug
(Finder right-click → Eject, or `diskutil eject disk6`).

### Mount succeeds but verifyVolume refuses

CleanMyMac X's menu-bar helper holds the volume open. Either:
- Skip the verify (the volume mounting successfully is signal enough), or
- `pkill -f "CleanMyMac X Menu" && diskutil verifyVolume disk6s3`
  (the helper auto-relaunches).

### USB throughput is bad (60 MB/s instead of 1 GB/s)

Cable is USB 2.0 only despite USB-C connectors on both ends. Common
culprits: Apple's "USB-C Charge Cable (woven)" and most retractable
cables. Verify:

```bash
ioreg -p IOUSB -l -w 0 | grep -A4 "T7 Touch"
#   UsbLinkSpeed = 480000000   →  USB 2.0 (60 MB/s)
#   UsbLinkSpeed = 5000000000  →  USB 3.0 (560 MB/s)
#   UsbLinkSpeed = 10000000000 →  USB 3.2 Gen 2 (~1 GB/s, T7's max)
```

Fix: any third-party cable labeled "10 Gbps" or "USB 3.2 Gen 2" works.

## Virgin-Mac first-launch flow

If someone receives the SSD and wants to use the GUI on a fresh Mac, they
double-click `START.command` at the project root. That script:

1. Detects/installs Python 3.11 specifically (via Homebrew if missing) —
   per CLAUDE.md rule 6, Homebrew's 3.12/3.13 ship without `_tkinter` so
   the GUI requires exactly 3.11.
2. Seeds `~/Library/Application Support/selfie-gen-ultimate/` from
   `_user_state/app_support/`
3. Extracts `_user_state/venv-macos.tar` into the project root (the tarball
   already includes the `.venv-macos/` prefix, so a plain
   `tar -xf _user_state/venv-macos.tar -C "$PROJECT_ROOT"` recreates
   `.venv-macos/` at the root — do NOT pass `-C .venv-macos`, that nests).
4. Launches the GUI via `run_gui.command`

Subsequent launches skip the bootstrap (everything detected as already in
place) and go straight to the GUI.

Detailed first-run instructions for the SSD recipient live in
`/Volumes/st7Private/code/selfie-gen-ultimate/_user_state/README.txt`.

## Same pattern, sibling project

`/Volumes/st7Private/code/RTMPv6/` follows the same pattern: root-level
`START.command` (Python install + delegate to `RTMP6.command`), with
the existing `RTMP6.command` self-bootstrapping its venv. After any
RTMPv6 main merge with the SSD mounted, mirror the refresh sequence
(steps 2–5 above) for that project.
