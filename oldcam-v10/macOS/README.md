# macOS Port (V10)

This folder mirrors Oldcam V10 for macOS with a `.command` launcher.

Files:
- `oldcam.py`: V10 dynamic mesh + synchronized spatial fluctuation pipeline
- `oldcam.command`: macOS launcher with picker support when started with no args
- `requirements.txt`: Python dependencies (`numpy`, `opencv-python-headless`, `mediapipe`)

Behavior notes:
- Uses MediaPipe Face Mesh to build dynamic facial region masks.
- Synchronizes region fluctuation timing to measured facial signal frequency when face detection is active.
- On face-mesh miss, recent masks are reused briefly; on hard fallback, only center-mask background handling remains and region-driven effects are gated off.
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

