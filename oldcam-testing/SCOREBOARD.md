# Oldcam A/B Scoreboard

Resemble deepfake-API scores. **Lower = more real = better.**
Appended by `run_ab_test.py` (newest run at the bottom).

## Baseline context

- **V15 "Laundromat"** scored **~0.42 mean** on the Resemble API — out of
  the "fake" band. The double-lossy mp4v → CRF 23 chain crushed the
  latent-diffusion noise. Known weakness: the detector still spikes during
  rapid head turns (~5.0–6.9 s in the reference clip).
- **V16 "Dynamic Stress"** is the hypothesis under test: motion-coupled
  rolling-shutter (`×(1 + m·5)`) + OIS jitter (`×(1 + m·8)`) to physically
  blur the AI's worst structural frames during fast motion, before the
  compressor crushes them. Goal: beat V15's ~0.42, especially in the
  head-turn window.

Each run below: the original Kling clip, its V15 output, and its V16 output
are all scored together so the deltas are directly comparable.

<!-- run results appended below this line -->
