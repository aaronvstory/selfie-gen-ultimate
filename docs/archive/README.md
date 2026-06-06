# docs/archive/ — historical session briefs

The files here are "handoff" / "kickoff" docs that were written as
self-contained briefs to start a **fresh Claude Code chat** on a multi-
hour project (the v2.17 unified GPU dep bootstrap, the v2.20 uv migration,
the v2.21 GPU rPPG fix, the v13 original handoff). They served their
purpose at the time and are kept here as a paper trail of how each
initiative was scoped.

**They are NOT current operating documentation.** The permanent rules
each one introduced have been folded into the active docs:

| Archived doc | Permanent rules now live in |
|---|---|
| `v2.17-unified-gpu-deps-handoff.md` | [`../macos-portability.md`](../macos-portability.md) (the constraints-thread pattern), [`../uv-migration.md`](../uv-migration.md) (the install-site contract), CLAUDE.md "numpy<2 / constraints.txt invariant" |
| `v2.20-uv-migration-kickoff.md` | [`../uv-migration.md`](../uv-migration.md) (full uv path + pip fallback contract + extras model) |
| `v2.21-gpu-rppg-and-uv-finish-handoff.md` | [`../uv-migration.md`](../uv-migration.md) (CuPy + nvidia component wheels), the GPU-bootstrap stamp logic in `scripts/gpu_bootstrap.py` |
| `v13-handoff.md` | [`../macos-portability.md`](../macos-portability.md), [`../cross-os-bounce-traps.md`](../cross-os-bounce-traps.md) (most of the early portability rules originated here) |

If you're tempted to use one of these as a session brief on a future
initiative: don't. Build a fresh brief that points at the current `docs/`
state. These are historical record only.
