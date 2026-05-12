# Oldcam V10 — "Spatial Sync" (macOS)

Builds on V9's face-aware pipeline with frequency-synchronized per-region spatial effects. Detects the dominant oscillation frequency in the video signal and phase-locks chroma and spatial fluctuations to it, producing more coherent and natural-looking sensor artifacts. Outputs `clip-oldcam-v10.mp4`.

## What It Does (beyond V9)

- **Base frequency detection:** FFT analysis on the green channel detects dominant motion frequency (0.7–4.0 Hz range) after ≥60 frames are processed.
- **Synchronized spatial fluctuation:** Per-region chroma and spatial shifts are phase-locked to the detected frequency. Each region has a phase offset (forehead: 0, cheeks: 0.15, chin: 0.25) for a natural cascading effect.
- **Dynamic relighting:** Gradient-based relighting simulates scene light changes in the direction opposite to OIS jitter movement.
- **Warm region detection:** YCrCb-based skin tone detection drives saturation modulation separately from the face mask.
- All V9 features are included: MediaPipe face detection, region masks, AWB drift, background blur, temporal smoothing, OIS jitter, rolling shutter.
- **Encoding:** H.264 `high` profile, CRF 18 — high quality, no bitrate ceiling.

## Requirements

- Python 3.11+
- `mediapipe==0.10.35`, `opencv-python-headless>=4.8.1.78`, `numpy>=1.24,<2`
- `face_landmarker.task` model file — place in the repo root or this directory.
  Download from [MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker).

## Files

| File | Purpose |
| --- | --- |
| `oldcam.py` | V10 processing pipeline |
| `oldcam.command` | macOS launcher — opens file picker if no args given |
| `requirements.txt` | Python dependencies |

## Setup

```bash
# Install deps (first run only — launcher does this automatically)
python3 -m pip install -r requirements.txt
python3 -m pip install --no-deps mediapipe==0.10.35

# Make launcher executable (once)
chmod +x oldcam.command
```

If macOS Gatekeeper blocks the `.command` file, right-click it, choose **Open**, then confirm once.

## Usage

**Double-click** `oldcam.command` — opens a file picker, processes selected files.

**CLI:**
```bash
python3 oldcam.py path/to/clip.mp4
python3 oldcam.py path/to/clip.mp4 --preview   # side-by-side comparison frame
```

Output is written next to the source file as `clip-oldcam-v10.mp4`.

## Note on Frequency Sync

Spatial sync requires a minimum of 60 frames to stabilize the FFT estimate. On shorter clips, effects still run but without frequency locking — behavior degrades gracefully to V9-like output.

