# uv Migration (v2.20)

This repo's dependency management moved from `pip` + `requirements.txt` +
`constraints.txt` + hand-rolled stamp/health/launcher machinery to
[`uv`](https://github.com/astral-sh/uv) with a committed `uv.lock`. The pip path
is **kept as an automatic fallback** (rollback safety) — uv is a fast-path, not
a hard replacement.

> **TL;DR for launchers:** every launcher tries `scripts/uv_sync_deps.py` first
> (one `uv sync` provisions the whole locked env). On any uv problem it returns
> exit 3 and the launcher falls through to the legacy pip install. Force pip
> with `KLING_USE_PIP=1`.

---

## Why uv

`uv sync` resolves the **entire locked dependency set in one shot** — the full
face stack (numpy<2, TF 2.16.2, mediapipe + its full runtime deps, opencv<4.12,
scipy, absl) plus GPU-aware torch/CuPy. That eliminates the v2.10–v2.17 class of
fresh-install bugs by construction:

- **No per-launcher subset.** The old design had ~30 install sites installing
  *divergent subsets*, patched after the fact by a brittle self-heal layer. Now
  every launcher runs the same `uv sync` against the same `uv.lock`.
- **No mediapipe `--no-deps` gap.** `uv.lock` pins numpy<2 globally, so
  mediapipe resolves with its FULL dep tree (matplotlib included) — the deep
  `mediapipe.tasks.python.vision` import (the recurring rPPG `-NORPPG` killer) is
  satisfied by the lock, not a post-install top-up.
- **No `-c constraints.txt` threading on the uv path.** The lock is the
  constraint for `uv sync`; numpy can't float to 2.x because the lock forbids
  it. The **pip fallback path stays in-tree and still requires `-c
  constraints.txt` at every pip-install site** — that rule is unchanged.
- **Reproducible + hashed.** `uv.lock` is cross-platform (locks per-marker) and
  carries hashes.

---

## The torch/CuPy GPU split (the hard part)

uv markers are STATIC (platform/arch); they can't express "this Windows box has
a working NVIDIA GPU." So the GPU-presence split is a **runtime** decision made
at launch:

| Machine | uv extra | torch source |
|---|---|---|
| Windows + NVIDIA, CUDA 13.x driver | `cu128` | `download.pytorch.org/whl/cu128` |
| Windows + NVIDIA, CUDA 12.x driver | `cu121` | `download.pytorch.org/whl/cu121` |
| Windows, no/broken NVIDIA | `cpu` | `download.pytorch.org/whl/cpu` |
| macOS (Apple Silicon) | `cpu` | PyPI default (MPS/CPU wheel) |

`scripts/uv_torch_select.py` makes that decision by **reusing the proven, unit-
tested** `gpu_bootstrap.detect_nvidia()` + `resolve_torch_mode()` (one decision
table, not two), then runs `uv sync --extra <X>`. After a CUDA sync it probes
`torch.cuda.is_available()` and **re-syncs `--extra cpu`** if the CUDA build is
runtime-broken (missing DLLs / driver mismatch / AV quarantine). torch CUDA only
affects SPEED (production never calls `torch.cuda.*`), so every failure path
degrades to a working CPU env, never a broken launch.

### CuPy CUDA components (the `[ctk]` no-op fix)

The v2.17 pip path requested `cupy-cudaNNx[ctk]`. **`[ctk]` is NOT a real extra
on cupy 13.6.0** (verified 2026-06-03 — cupy 13.x publishes only `all` + `test`;
both pip and uv silently drop `[ctk]` with a warning and pull only numpy +
fastrlock). So the CUDA runtime DLLs (nvrtc etc.) never landed via `[ctk]` — a
latent GPU-rPPG bug on CUDA-12 boxes. `pyproject.toml` instead pins the CUDA
component wheels **explicitly**: the un-suffixed `nvidia-*` line (`nvidia-cuda-
nvrtc`, `nvidia-cublas`, ...) for CUDA 13.x, the `-cu12` line for CUDA 12.x.
`rPPG/rppg_injector.py`'s `os.add_dll_directory()` registration (unchanged) now
has DLLs to find.

CuPy stays pinned `>=13.6,<14`: CuPy 14.x requires numpy>=2.0, mutually
exclusive with the numpy<2 face stack.

---

## Files

| File | Role |
|---|---|
| `pyproject.toml` | dependency manifest + torch index routing + `required-environments` |
| `uv.lock` | committed cross-platform lockfile (win-AMD64 + darwin-arm64 + linux-x64) |
| `scripts/ensure_uv.py` | bootstraps uv if absent (PowerShell/winget on Win; install.sh/brew on Mac) |
| `scripts/uv_torch_select.py` | launch-time GPU→extra decision + sync + CUDA probe/fallback |
| `scripts/uv_sync_deps.py` | the canonical orchestrator the launchers call (exit 0 = ready, 3 = pip fallback). Sets `UV_HTTP_TIMEOUT=900` |
| `scripts/win_uv_sync.bat` | Windows launcher helper (CRLF) |
| `scripts/uv_sync.sh` | macOS/Linux launcher helper (LF, sourced) |

The canonical venv is shared: uv syncs into `venv\` (Windows) / `.venv-macos\`
(macOS) — the SAME dir the pip path uses — so the GUI launches one interpreter
regardless of which path provisioned it.

---

## Cross-OS wheel-gap notes (encoded in `required-environments`)

`tool.uv.required-environments` forces the lock to resolve for win-AMD64 +
darwin-arm64 + linux-x64, catching wheel gaps at lock time:

- **`tensorflow-io-gcs-filesystem`** has no Windows wheel past 0.31.0 and no
  Apple-Silicon wheel at 0.31.0 → split: win pins `==0.31.0`, mac/linux float
  `>=0.37.1`.
- **CuPy** ships only win/linux wheels → cu121/cu128 extras gate cupy + the
  nvidia components `sys_platform != 'darwin'`; macOS uses the `cpu` extra.
- **Intel macOS (darwin x86_64) is intentionally unsupported** — mediapipe
  0.10.35 has no darwin-x86_64 wheel (Apple Silicon only). Matches reality.

---

## Verification

- Always-on real-import probes: `pytest tests/test_uv_lock_imports.py -q`
  (numpy<2, opencv<4.12, deep mediapipe Tasks-API, scipy, absl, torch, TF
  legacy-keras).
- Fresh `uv sync` install + deep import (slow, network):
  `RUN_UV_SYNC_TEST=1 UV_SYNC_TEST_EXTRA=cu128 pytest
  tests/test_uv_lock_imports.py::test_uv_sync_extra_real_import -q`
- Selector + orchestrator wiring: `pytest tests/test_uv_torch_select.py
  tests/test_uv_sync_deps.py tests/test_uv_launcher_integration.py -q`

---

## Rollback

The uv migration shipped in v2.20 (PR #71). `requirements.txt` + `constraints.txt`
are kept in-tree as
the pip fallback; the launchers degrade to them automatically when uv is absent
or fails (`KLING_USE_PIP=1` forces it). They'll be retired only once uv is
proven on both OSes in production.

---

## The `dev` extra contract — installing pytest (added v2.24)

> **Source of the code change:** the `dev` extra landed via
> [PR #79](https://github.com/aaronvstory/selfie-gen-ultimate/pull/79)
> (`feat/macos-polish-post-v2.21`), commit `de161c04`, in the v2.24
> release round. This section is the matching contract doc.

CLAUDE.md's pre-commit invariant is `pytest tests/ similarity/tests/ -q`.
But pytest is NOT installed by the end-user launcher path — `run_gui` /
`run_cli` / `run_auto` all run `uv sync --no-default-groups --extra
<cpu/cu*>`, which intentionally keeps end-user envs lean.

Contributors opt-in to pytest explicitly via the `dev` extra:

```bash
# macOS / CPU-only
uv sync --extra cpu --extra dev

# Windows + CUDA 13.x
uv sync --extra cu128 --extra dev

# Windows + CUDA 12.x
uv sync --extra cu121 --extra dev
```

The `dev` extra lives in `[project.optional-dependencies]` of both
`pyproject.toml` (source of truth) and `distribution/pyproject.toml`
(packaging mirror — kept in spec-equality lockstep by
`tests/test_macos_dev_extra_provides_pytest.py`).

**When you add a tool to the pre-commit invariant** — pytest plugins,
linters, type checkers, anything CLAUDE.md or
[`pr-review-loop.md`](pr-review-loop.md) (where the full invariant list
lives post-v2.24) tells a contributor to run — declare it in the `dev`
extra in BOTH pyproject
files and re-resolve the lock. Otherwise the next `uv sync` actively
UNINSTALLS the tool from the venv (uv's `--no-default-groups --extra X`
prunes anything not in extra X), and a fresh-clone contributor on
macOS hits the gap immediately.

The pre-v2.24 state: pytest was undeclared in pyproject / requirements
/ uv.lock. Every macOS `uv sync` removed pytest from `.venv-macos`. The
Windows author had pytest ambient via system Python so the gap was
invisible. v2.24 closed it via the `dev` extra above. See
`tests/test_macos_dev_extra_provides_pytest.py` for the regression
guard.

### Why not `[dependency-groups]` instead?

uv 0.5+ supports a `[dependency-groups]` table that holds dev/test
dependencies separately from `optional-dependencies`. We use the
`dev` *extra* (under `optional-dependencies`) instead because:

- The launchers already pass `--no-default-groups` to skip groups, and
  changing them would mean threading `--group dev` through every
  launcher path and the CI matrix. Extras already work with the
  existing flag set.
- `dependency-groups` is uv-specific; sticking to extras keeps the
  pyproject pip-installable (`pip install .[dev]`) without uv. The
  pip fallback path is still load-bearing per the rollback section
  above.

If/when the dev/optional separation matters more (a new `test`
extra that depends on `dev`, for instance), revisit.
