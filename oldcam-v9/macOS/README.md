# macOS Port (V9)

This folder mirrors Oldcam V9 for macOS with a `.command` launcher.

Files:
- `oldcam.py`: V9 dynamic mesh pipeline (MediaPipe Face Mesh + soft motion/noise/tone stages)
- `oldcam.command`: macOS launcher with picker support when started with no args
- `requirements.txt`: Python dependencies (`numpy`, `opencv-python-headless`, `mediapipe`)

Behavior notes:
- Uses dynamic face-region masks from MediaPipe mesh for region-aware processing.
- If face mesh is temporarily missing, it reuses recent masks briefly.
- If detection is lost, it falls back to a center ellipse for background-only compression and disables region-driven effects (`face_detected=False` gate).
- No landmarks, overlays, or debug watermarks are rendered to output.

Setup:
1. Install Python 3.9+.
2. Install deps:
   `python3 -m pip install -r requirements.txt`
3. Ensure launcher is executable (once):
   `chmod +x oldcam.command`

Usage:
- Double-click `oldcam.command` and choose files.
- Or run directly:
  `python3 oldcam.py clip.mp4`

