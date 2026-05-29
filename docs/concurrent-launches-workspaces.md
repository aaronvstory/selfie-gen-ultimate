# Concurrent launches & workspaces (PR #49)

> **Relocated from `CLAUDE.md` (2026-05-29) to reduce always-loaded context.**
> Still binding. Read this file BEFORE adding any new file the GUI writes at
> runtime, or touching workspace/instance/runtime-dir logic.

The GUI is safe to launch multiple times concurrently. Each process gets an
isolated runtime directory keyed by `<YYYYMMDD-HHMMSS>-<PID>`, so carousel
state, video history, and crash logs from one window never bleed into
another. Generated image / video files were already source-folder-adjacent
(per `path_utils.get_gen_*_folder`) — they were never the bleed culprit.

## Classification of on-disk state

| State                                              | Scope        | Path |
|----------------------------------------------------|--------------|------|
| `kling_config.json` (API keys, prompts, models)    | **Shared**   | `<user_data_root>/kling_config.json` |
| `ui_config.json` (window geometry, sash positions) | **Shared**   | `<user_data_root>/ui_config.json` |
| `kling_gui.log` (rotating log)                     | **Shared**   | `<user_data_root>/kling_gui.log` |
| `crash_log.txt` (init / runtime crash sink)        | Per-instance | `<runtime>/crash_log.txt` |
| `kling_history.json` (video generation history)    | Per-instance | `<runtime>/kling_history.json` |
| `<key>_autosave.json` (rolling carousel autosave)  | Per-instance | `<runtime>/sessions/<key>_autosave.json` |
| Manual session saves                               | **Shared**   | `<user_data_root>/sessions/<name>.json` |
| Liveness marker (one per running window)           | Per-workspace | `<workspace>/runtime/.markers/<instance>.json` |

`<runtime>` = `<workspace_dir>/runtime/instances/<instance_id>/`.
`<workspace_dir>` = `<user_data_root>` for the default workspace, or
`<user_data_root>/workspaces/<name>/` for a named workspace. `<user_data_root>`
follows the existing platform branch: `~/Library/Application Support/selfie-gen-ultimate/`
on macOS, `<app_dir>` on Windows (preserves the portable workflow).

**Shared files are last-writer-wins.** Each instance reads them once at
startup and keeps an in-memory copy — there is no live-sync between
windows. A save from window A simply overwrites the file; window B keeps
its in-memory copy until the next launch. This is intentional: API keys,
prompts, and model selections rarely change mid-session, and the cost of
per-window config (API-key re-entry, layout reset) outweighs the benefit.

## Launching with a named workspace

```bash
# macOS — args forward through the full launcher chain (PR #49)
./run_gui.command --workspace shoot-a
KLING_WORKSPACE=shoot-a ./run_gui.command

# Windows
run_gui.bat --workspace shoot-a
set KLING_WORKSPACE=shoot-a && run_gui.bat
```

Workspace names must match `^[A-Za-z0-9._-]+$`, be ≤64 chars, must not start
with `.`, and must not be Windows reserved device names. Invalid names fall
back to `default` with a stderr warning.

## Env vars set per launch

- `KLING_WORKSPACE` — sanitized workspace name. Always set after bootstrap.
- `KLING_INSTANCE_ID` — `<YYYYMMDD-HHMMSS>-<PID>`. Auto-generated if absent;
  inherited by subprocesses so a future helper-process can find its parent.

## Design rule for new on-disk state

When you add any new file the GUI writes at runtime, classify it BEFORE
choosing a path:

- **Shared** (cross-instance read/write): use `path_utils.get_user_data_dir()`
  or one of the existing `get_*_path` helpers. Accept last-writer-wins or
  add explicit locking; document the choice in this section.
- **Per-instance** (private to one window): use `path_utils.get_runtime_dir()`
  / `get_runtime_sessions_dir()` / etc. Survives orderly close; cleaned up
  on next launch via stale-marker sweep.

Don't introduce new shared writable files without an explicit note here.
The bleed bug PR #49 fixed started exactly this way — `sessions/<key>_autosave.json`
was a shared file that two windows would overwrite each other on.

## Bootstrap mutex (`.launcher_state/setup.lock`)

The shell and batch launchers acquire a `mkdir`-based atomic lock around
the dependency-setup phase (`setup_macos.sh` on macOS, the `:INSTALL_REQUIREMENTS`
block on Windows) so two concurrent first-launches don't race on `pip install`
and corrupt the shared venv. The lock is **released BEFORE the GUI is
launched** — multiple GUI windows then run concurrently with no shared lock.

Stale-lock cleanup: macOS uses a 10-minute window (most dep installs finish
in 2-3 min); Windows uses a more conservative 1-day window (batch arithmetic
on file mtimes is awkward). If you ever need to force-clear the lock:
`rm -rf .launcher_state/setup.lock` (macOS) / `rd /S /Q .launcher_state\setup.lock`
(Windows). Don't extend the lock to cover GUI-process lifetime — that
defeats the entire concurrent-launches feature.
