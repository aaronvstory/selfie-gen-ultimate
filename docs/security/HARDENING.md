# Supply Chain Hardening — Operational Guide

> Companion to `docs/security/SUPPLY_CHAIN_THREAT_MODEL.md`. Read that first
> for the WHY. This file is the HOW.

## TL;DR commands

```bash
# One-time setup (per dev)
python -m pip install -U pip pip-audit pip-tools

# Audit dependencies (run on every dependency change)
scripts/audit_deps.sh        # macOS / Linux
scripts\audit_deps.bat       # Windows
# Or directly (range-pin-safe — DO NOT pass --no-deps with
# range-pinned requirements; pip-audit refuses ranges in that mode):
python -m pip_audit -r requirements.txt --strict --progress-spinner off
# For fully-pinned hashed input (preferred):
python -m pip_audit -r requirements-hashed.txt --strict --require-hashes

# Check for IoCs (run if you suspect compromise OR weekly)
python scripts/detect_compromise.py
```

## 1. Tools

### 1.1 `pip-audit` (PyPA — recommended)

Checks every installed/declared package against the PyPA Advisory Database
and the OSV database. Maintained by Trail of Bits with Google support.

```bash
# Install
python -m pip install pip-audit

# Audit a requirements file (offline-ish — uses cached advisory DB)
python -m pip_audit -r requirements.txt --strict

# Audit a live venv
python -m pip_audit --strict

# Audit + auto-fix (creates a new requirements file with safe versions)
python -m pip_audit -r requirements.txt --fix --dry-run
```

**Tradeoffs:**

- ✅ Free, fast, maintained by PyPA
- ✅ Cross-checks PyPA DB + OSV DB
- ⚠️ Finds KNOWN advisories — doesn't catch a brand-new compromise during
  its 6-12 minute detection window (per Socket.dev's median detection time
  for Mini Shai-Hulud)
- ⚠️ False positives possible on transitive deps that are unreachable code
  paths in our project

### 1.2 OSV-Scanner (Google)

Second source for vulnerability data. Useful if pip-audit misses a CVE that
osv.dev has indexed first.

```bash
# Install via go (one-time)
go install github.com/google/osv-scanner/cmd/osv-scanner@latest

# Or download a binary release from https://github.com/google/osv-scanner/releases
# Windows: PowerShell-friendly release exists

# Scan
osv-scanner --lockfile=requirements.txt
osv-scanner --lockfile=similarity/requirements.txt
osv-scanner --lockfile=oldcam-v24/requirements.txt
# ... etc for each oldcam-v* requirements.txt
```

**Tradeoffs:**

- ✅ Free, Google-maintained
- ✅ Covers more ecosystems than pip-audit (cross-references npm, Go, Rust)
- ⚠️ Go binary requires a separate install step
- ⚠️ Same blind spot for fresh compromises in the 6-12 min detection window

### 1.3 GitHub Dependabot (built-in, free)

Repo-level automated security update PRs. Configured in
`.github/dependabot.yml`. Free for all GitHub repos including private.

**Tradeoffs:**

- ✅ Zero infra — runs in GitHub
- ✅ Opens PRs automatically; you review + merge
- ⚠️ Only opens PRs for CVEs in the GitHub Advisory DB (subset of OSV)
- ⚠️ Default cadence is "as new advisories land" + a weekly digest; not real-time

### 1.4 Socket / Snyk (paid, optional)

- **Socket.dev** — has a free tier for public repos. Specializes in supply
  chain attacks (detected Mini Shai-Hulud at median 6.7 minutes per their
  blog). Paid for advanced features.
- **Snyk** — paid for non-trivial use. Excellent CVE coverage + a CLI.

**Tradeoffs:**

- ✅ Best-in-class detection latency (Socket flagged TanStack worm within
  6-12 minutes of publication)
- ⚠️ Cost — small projects use the free tier; teams pay
- ⚠️ Cloud-hosted scanning means you upload your manifest

## 2. Hash pinning

Why: pinning by version number doesn't detect tampering. A compromised
package republished under the same version with a different payload still
installs without warning. SHA-256 hash verification catches this at install
time — if `pip` can't match the hash, install fails.

### 2.1 Generate a hashed requirements file

```bash
# Install pip-tools (one-time)
python -m pip install pip-tools

# Compile requirements.txt → requirements-hashed.txt with hashes for ALL
# transitive deps. Pin Python version to match production.
python -m piptools compile \
  --generate-hashes \
  --output-file=requirements-hashed.txt \
  requirements.txt

# Same for similarity subproject
python -m piptools compile \
  --generate-hashes \
  --output-file=similarity/requirements-hashed.txt \
  similarity/requirements.txt
```

### 2.2 Install with hash checking

```bash
# Production / CI installs — pip refuses any package whose hash differs
python -m pip install --require-hashes -r requirements-hashed.txt

# Equivalent in poetry: poetry config installer.modern-installation true
#                      poetry install --no-root --sync
# Equivalent in uv:    uv pip sync requirements-hashed.txt
#                      (uv enforces hashes by default if present)
```

### 2.3 Keep hashes fresh

When you update a direct dep version in `requirements.txt`, re-run
`piptools compile` to regenerate the hashed file. **Don't edit the hashed
file by hand** — it's generated and signed by pip-tools.

**Tradeoffs:**

- ✅ Detects any post-publish tampering
- ✅ Reproducible installs (same hash → same bytes)
- ⚠️ Some friction: every dep update requires a regen step
- ⚠️ Doesn't help if the original package was malicious from the start
  (pip-audit / OSV catches that)

## 3. Prefer binary wheels (`--only-binary :all:`)

Why: sdist packages (`*.tar.gz`) run `setup.py` at install time, which is
arbitrary Python code. Binary wheels (`*.whl`) don't — they unpack a
pre-built artifact with no install-time execution.

```bash
# Force wheels for everything; fail rather than fall back to sdist
python -m pip install --only-binary :all: -r requirements.txt

# Or per-package (mediapipe is the awkward one)
python -m pip install --only-binary :all: --no-deps mediapipe==0.10.35
```

**Tradeoffs:**

- ✅ Eliminates setup.py-as-attack-vector for the entire dep tree
- ⚠️ Some packages don't ship wheels for niche platforms (e.g. ARM Linux);
  install will fail rather than fall back
- ⚠️ Wheels can still embed malicious code — they just can't execute at
  install time. The package still runs when you `import` it.

The current launchers (`launchers/windows/run_gui.bat`,
`launchers/macos/run_gui.command`) already use `--only-binary :all:` for the
main pip install + a `--no-deps` install for mediapipe. ✅

## 4. CI/CD hardening

### 4.1 Run pip-audit + OSV-Scanner on every PR

`.github/workflows/supply-chain-audit.yml` is the workflow. Runs on every
push + PR. Fails CI if a NEW advisory hits a dep we use.

### 4.2 Least-privilege tokens

- **Default GITHUB_TOKEN** — set `permissions: {contents: read}` at the
  workflow level. Step-level escalation only where required (e.g.
  `pull-requests: write` for the bot to comment).
- **Long-lived PATs** — none. Repository secrets only for what's strictly
  needed.
- **OIDC** — for any cloud auth (fal.ai, AWS, GCP), use OIDC token exchange
  instead of long-lived keys when the provider supports it. fal.ai doesn't
  yet; AWS/GCP do.

### 4.3 Ephemeral runners

GitHub-hosted runners are already ephemeral (fresh VM per job). Self-hosted
runners SHOULD also be ephemeral — never persistent.

## 5. Sandboxed installs

Use `scripts/sandbox_install.sh` / `.bat` to install dependencies in an
isolated venv that has no access to your shared cloud creds. This is the
"defense in depth" layer — even if a package goes malicious during install,
it can only see what's inside the sandbox.

```bash
# Linux/macOS
scripts/sandbox_install.sh

# Windows
scripts\sandbox_install.bat
```

Both create a fresh venv under `.sandbox-venv/`, install with
`--only-binary :all:` (refuses to fall back to sdist), then run pip-audit
against the installed tree. If `requirements-hashed.txt` exists at the
repo root (it's committed — see §2 below), they install with
`--require-hashes` too so post-publish tampering on any pinned dep
fails the install instead of running its payload. If you've updated
`requirements.txt` but not yet regenerated the hashed file, the
sandbox falls back to the unhashed install and logs a warning. They
do NOT unset environment creds — that's the user's job
(`unset AWS_PROFILE GCP_...`) before running.

## 6. Disabling pre/post install hooks

Python doesn't have npm-style `preinstall` scripts. The equivalents are:

- **`setup.py` execution** — disabled by `--only-binary :all:`.
- **`.pth` files in site-packages** — can't be disabled wholesale. Audit
  them. `scripts/detect_compromise.py` flags any `.pth` file containing
  executable code (not just a path).
- **`pyproject.toml` build-backend hooks** — these only run during sdist
  install. Same mitigation as setup.py.
- **`entry_points` console scripts** — these run only when the user invokes
  them. Awareness only.

## 7. Update review process

For any dependency update (direct or transitive):

1. **Read the changelog** (PyPI release notes + GitHub releases page).
2. **Diff the published files** if the package is small — `pip download`
   the new version + the old, `diff -r` the unpacked trees.
3. **Run `pip-audit` against the proposed update** before committing.
4. **Wait 24-48h** after a release before pulling unless the update fixes
   a CVE we're affected by. Most supply chain attacks are detected within
   the first 24h.

## 8. What to do if pip-audit finds something

1. **Read the advisory**. Most pip-audit hits are KNOWN CVEs in transitive
   deps that may not affect us if we don't call the vulnerable code path.
2. **If the CVE is real for our usage**, update to the patched version:
   ```bash
   python -m pip install --upgrade <package>==<safe_version>
   ```
3. **Regenerate the hashed requirements file** (see §2.3).
4. **Run the full test suite + smoke-test the GUI.**
5. **Commit + PR.**

## 9. What to do if `detect_compromise.py` finds something

See `docs/security/IOC_DETECTION_CHECKLIST.md` — that's the incident response
runbook. The summary: assume creds are compromised, rotate everything,
revoke all PATs/tokens, isolate the machine, file a report.

## 10. Free vs paid summary

| Tool | Free | Paid features | Recommended for us |
|---|---|---|---|
| pip-audit | ✅ Full | n/a | ✅ Required |
| OSV-Scanner | ✅ Full | n/a | ✅ Required |
| GitHub Dependabot | ✅ Full | GitHub Advanced Security ($$) for org policies | ✅ Enabled |
| Socket.dev | Free tier (public repos) | Paid for org/private + faster scanning | Recommended add |
| Snyk | Free tier (CLI + ~200 tests/mo) | Paid for unlimited + UI | Optional |
| Tidelift | Paid only | n/a | Skip (overkill for us) |

## 11. Pre-commit hook installer

`.git/hooks/` is not tracked by git, so a fresh clone (e.g. on a new
macOS box) does NOT get the supply-chain audit pre-commit hook
automatically. Install it with:

```bash
bash scripts/install-precommit.sh
```

The hook is:
- **Fast** (~240ms) on commits that don't touch dep manifests — it
  short-circuits via a pattern match on staged file paths.
- **Slower** (~45s, project audit only) on commits that touch
  `requirements*.txt`, `package*.json`, lockfiles, `pyproject.toml`,
  `.github/workflows/*.yml`, `.pth` files, or `.claude/*.{js,mjs,json}`.
- **Off** for the machine-wide scan by default (that's ~5min per
  commit, kept out of the commit path; runs nightly in CI instead).

Override knobs:
- `SHAI_HULUD_FORCE=1 git commit ...` — run the full project scan
  even when no dep files changed (e.g. before a release tag).
- `SHAI_HULUD_MACHINE_AUDIT=1 git commit ...` — also run the
  ~5min machine-wide OSV.dev scan. Only useful on a machine that
  has `~/.shai-hulud/shai-hulud-audit.sh` installed.

## 12. Safe-install wrappers

`pip install` and `npm install` are the **actual** attack moment
for the Shai-Hulud / TeamPCP campaign — a compromised package
executes its `postinstall` script (npm) or `setup.py` (pip sdist)
the instant it lands on disk. A pre-commit hook can't help there;
the damage is already done by the time you `git commit`.

The wrapper scripts run the project audit *immediately after* the
install finishes, while the bad code is still freshly written and
detectable:

```bash
# macOS / Linux / Git Bash
./scripts/safe_install.sh pip install some-pkg
./scripts/safe_install.sh npm install some-pkg

# Windows cmd / PowerShell
scripts\safe_install.bat pip install some-pkg
scripts\safe_install.bat npm install some-pkg
```

The wrappers do NOT replace `pip` or `npm` globally — you opt into
them per-command. For frequent use, define a shell alias:

```bash
# ~/.bashrc / ~/.zshrc
alias pipi='./scripts/safe_install.sh pip install'
alias npmi='./scripts/safe_install.sh npm install'
```

```cmd
REM Windows doskey (only for current cmd session)
doskey pipi=scripts\safe_install.bat pip install $*
doskey npmi=scripts\safe_install.bat npm install $*
```
