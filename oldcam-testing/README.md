# oldcam-testing — V16 "Dynamic Stress" bench

Throwaway experiment area. **Not wired into the app** (no launcher in
`launchers/`, no `_discover_oldcam_versions` entry, no `config_panel`
checkbox, not in the main pytest suite). It exists to answer one question
before we invest in integrating V16 everywhere:

> Does V16's motion-coupled optical stress score better than V15 on the
> Resemble deepfake API?

## What's here

| File | Purpose |
|------|---------|
| `oldcam_v16.py` | Standalone V16. = V15 (mp4v + CRF 23 + ghosting 0.18 + zero noise) **plus** motion-coupled rolling-shutter (`shear × (1 + m·5)`) and OIS jitter (`offset × (1 + m·8)`), where `m` is a 0–1 frame-to-frame motion velocity. At `m == 0` (subject still) it is byte-behaviour-identical to V15. |
| `run_ab_test.py` | Make → score → compare harness. |
| `run_ab_test.bat` | Windows wrapper (uses the repo venv). |
| `SCOREBOARD.md` | Auto-appended ranked results, newest at the bottom. |
| `runs/` | Per-run output folders (gitignored). |

## The V16 idea

V15's double-lossy "Laundromat" compression scored ~0.42 (out of the "fake"
band) by crushing latent-diffusion noise. But the detector still spikes
during rapid head turns — AI struggles with structural consistency across
fast perspective shifts. V16 measures per-frame pixel velocity (grayscale
`absdiff` → threshold → normalized pixel count) and **dynamically spikes**
rolling-shutter shear and OIS jitter only during high-motion moments,
physically blurring the AI's worst structural frames right before the mp4v
compressor crushes them. When the subject is still it behaves exactly like
V15, so the only thing under test is the motion reaction.

## Quick A/B loop

```bat
:: Windows — make V15 + V16 from a Kling clip, score both + the original,
:: append the ranked result to SCOREBOARD.md
oldcam-testing\run_ab_test.bat "F:\path\to\kling_clip.mp4"
```

```bash
# Direct (any OS with the venv active)
python oldcam-testing/run_ab_test.py "F:/path/to/kling_clip.mp4"

# Just produce the files, score later by hand:
python oldcam-testing/run_ab_test.py clip.mp4 --no-score

# Only one variant, or add more clips:
python oldcam-testing/run_ab_test.py a.mp4 b.mp4 --variants v16
```

Each run:

1. Copies the source + writes `…-oldcam-v15.mp4` and `…-oldcam-v16.mp4`
   into `oldcam-testing/runs/run_<timestamp>/`.
2. Calls `resemble-score`'s CLI on that folder (`--cli --all`), which scores
   every clip through the Resemble API and writes `resemble_results.*`.
3. Appends a ranked table to `SCOREBOARD.md` (🏆 = lowest = best).

## Requirements

- `RESEMBLE_API_KEY` discoverable by resemble-score — see
  `resemble-score/README.md` (`.env`, external `.env`, or env var).
- `ffmpeg` on `PATH` (the oldcam final H.264 encode needs it).
- The repo Python env with `opencv-python` + `numpy` (the oldcam deps).

## Reading the result

`SCOREBOARD.md` lists every clip per run, lowest score first. If
`…-oldcam-v16.mp4` ranks **above** `…-oldcam-v15.mp4`, the motion-coupling
helped on that clip — then we decide whether to promote V16 into the app
proper (launchers, discovery, config, tests, macOS twin) via the normal
oldcam-wiring checklist. If it doesn't beat V15, we tweak the `×5` / `×8`
motion gains here and re-run, no app churn.
