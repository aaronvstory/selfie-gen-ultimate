# Oldcam V9 — "Dynamic Mesh" (macOS)

Applies face-aware virtual camera effects to videos. Uses MediaPipe to detect facial regions and applies per-region noise, tone, and motion effects. Outputs `clip-oldcam-v9.mp4` alongside the source.

## What It Does

- **Face detection:** MediaPipe FaceLandmarker (468 landmarks) drives per-region masks for forehead, cheeks, and chin.
- **Region-aware processing:** Each facial region receives independently tuned noise, tone mapping, and saturation.
- **AWB drift:** Subtle color temperature wander simulates auto white balance hunting.
- **Background blur:** Soft Gaussian texture applied outside face mask simulates focus fall-off.
- **Temporal smoothing:** Noise fields and masks blend across frames (85% previous + 15% fresh) for natural flicker.
- **OIS jitter + rolling shutter:** Soft micro-jitter with velocity damping.
- **Encoding:** H.264 `high` profile, CRF 18 — high quality, no bitrate ceiling.

## Requirements

- Python 3.11+
- `mediapipe==0.10.35`, `opencv-python-headless>=4.8.1.78`, `numpy>=1.24,<2`
- `face_landmarker.task` model file — place in the repo root or this directory.
  Download from [MediaPipe Face Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/face_landmarker).

## Files

| File | Purpose |
| --- | --- |
| `oldcam.py` | V9 processing pipeline |
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

Output is written next to the source file as `clip-oldcam-v9.mp4`.

