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

The harness ranks by **frame mean** (Resemble per-frame deepfake
probability) — the discriminating signal. The coarse top-level
`video_metrics.score` (the "~0.42" figure) rounds to Fake for almost any
AI clip, so frame-mean is what we compare.

## Findings

### 2026-05-17 — V16 "Dynamic Stress" — REGRESSION (rejected)

| | Top score | Frame mean | Frame max |
|--|-----------|-----------|-----------|
| **V15** (Laundromat, champion) | 0.4245 | **0.1605** 🏆 | 0.4245 |
| **V16** (motion-coupled, ×8 OIS / ×5 RS) | 0.5283 | 0.1884 | 0.5374 |

V16 ranked **#2, behind V15** (frame-mean +0.028 worse). The motion
coupling *raised* the peak frame score (0.42 → 0.54): the aggressive
geometric warping during the head turn introduced its own detectable
artifacts instead of masking the AI's. Both still crush everything else
(next best, v12, is 0.63). **Decision: V15 stays the production champion.**
Next idea to bench here: gentler motion gains (e.g. ×2 / ×1.5) or a
motion-gated *blur* instead of geometric shear/translation.

<!-- run results appended below this line -->

## 2026-05-17 14:28 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |

## 2026-05-17 14:29 — `front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4`

| Rank | Video | Frame mean | Frame min | Certainty | Verdict |
|------|-------|-----------|-----------|-----------|---------|
| 1 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15.mp4 | 0.1605 🏆 | 0.0035 | 0.1510 | Neutral/Uncertain |
| 2 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v16.mp4 | 0.1884 | 0.0034 | 0.0566 | Neutral/Uncertain |
| 3 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v12.mp4 | 0.6262 | 0.0610 | 0.9740 | Fake |
| 4 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v13.mp4 | 0.6597 | 0.0481 | 0.9844 | Fake |
| 5 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v8.mp4 | 0.8543 | 0.4395 | 1.0000 | Fake |
| 6 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v15-v1.mp4 | 0.9892 | 0.8990 | 1.0000 | Fake |
| 7 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4 | 0.9936 | 0.9443 | 1.0000 | Fake |
| 8 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v14.mp4 | 0.9953 | 0.9639 | 1.0000 | Fake |
| 9 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v7.mp4 | 0.9983 | 0.9871 | 1.0000 | Fake |
| 10 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v10.mp4 | 0.9993 | 0.9953 | 1.0000 | Fake |
| 11 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v9.mp4 | 0.9993 | 0.9926 | 1.0000 | Fake |
| 12 | front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1-oldcam-v11.mp4 | 0.9997 | 0.9980 | 1.0000 | Fake |
