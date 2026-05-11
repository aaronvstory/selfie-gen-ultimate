# macOS Port (V7)
# macOS Port (V8)

This folder mirrors the V8 Python functionality for macOS and replaces the Windows batch launcher with a `.command` launcher.

Files:
- `oldcam.py`: V8 processing script with temporal OIS jitter, random-velocity rolling shutter, shadow chroma noise, and H.264 bit-starved video finalization
- `oldcam.command`: macOS launcher with drag-and-drop support and an AppleScript file picker when launched empty
- `requirements.txt`: minimal Python dependencies

Setup on Mac:
1. Install Python 3.
2. Install dependencies:
   `python3 -m pip install -r requirements.txt`
3. Make the launcher executable once if needed:
   `chmod +x oldcam.command`

Usage:
- Double-click `oldcam.command` and choose files in the picker.
- Or drag files onto `oldcam.command`.
- Or run the Python script directly:
  `python3 oldcam.py clip.mp4 --ghosting 0.08`

Notes:
- The launcher probes for a Python interpreter that can import both `cv2` and `numpy`.
- You can force a specific interpreter with `OLDCAM_PYTHON=/path/to/python3 ./oldcam.command`.
- It accepts `OLDCAM_EXTRA_ARGS` for optional extra CLI flags.
- It accepts `OLDCAM_NO_PAUSE=1` to suppress the final prompt in automation.
