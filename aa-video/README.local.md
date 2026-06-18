# aa-video — Adversarial-Attack Video Re-Encode (isolated subproject)

Optional 4th post-processing step for selfie-gen-ultimate, alongside oldcam /
rPPG / crush. Applies adversarial perturbations + synthetic capture artifacts
engineered to evade AI-generated-video / deepfake detectors. **Authorized
red-team / detector-research use only.**

## Why it's isolated

This tool's deps (`numpy>=2`, `opencv>=4.10`, optional `torch`) **conflict**
with the main repo invariant (`numpy<2` / `opencv<4.12` / `TF 2.16.2`). So
UNLIKE rPPG/oldcam — which run off the shared main venv — aa-video runs in its
**own uv venv** at `aa-video/.venv`, owned by the launcher. Nothing here can
reach the main face stack.

## Pipeline slot

`Kling → rPPG → Loop → Crush → AA → Oldcam`. Each selected AA attack-pipeline
produces its own output file, which then fans through Oldcam (like crush tiers).

## Attack pipelines (fan-out selectable)

| Name | Chain | Targets |
|------|-------|---------|
| **prime** (default) | pixel → temporal → trace → recompress | generic AI-vs-real classifiers |
| scenario1 | micro_jitter → analog_sim → camera_recapture → recompress | replay/pre-recorded (DSP-FWA + FTCN) |
| scenario3 | sensor_noise → motion_mod → frame_chaos → recompress | smoothing/puppeteering (AltFreezing) |

CLI also exposes `all`, `v3_full`, `v3_light`, individual single-attacks, and
per-generator profiles (`--generator kling|seedance|runway|generic`).

## Standalone use

```bash
# macOS / Linux
./aa_launcher.sh --input clip.mp4 --attack prime --strength 0.5

# Windows
aa_launcher.bat --input clip.mp4 --attack prime --strength 0.5
```

The launcher ensures uv, syncs `aa-video/.venv` from `pyproject.toml` + `uv.lock`
(one-time, idempotent), then runs `main.py`. Output: `{stem}_{attack}_{strength}{ext}`.

## v1 scope

- **CPU-only** (no torch). Clips are short; `prime` needs only
  cv2/numpy/moviepy/scipy + a system **ffmpeg** binary. The `gpu` extra
  (torch/insightface/face-alignment) is opt-in and NOT in the default lock.
- `watermark` attack is disabled (needs the old `moviepy.editor` API, dropped
  in moviepy 2.x) — `prime`/`scenario*` don't use it.

## macOS note

The default lock is CPU-only so darwin resolves with zero CUDA wheels. The
`gpu` extra's `[tool.uv.sources]` marker excludes darwin, so even an opt-in GPU
sync falls through to the MPS/CPU torch wheel — **never pass a `cu*` extra**.
