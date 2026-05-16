# resemble-score

Scan a folder of generated videos, pick which ones to test, and score them
through the [Resemble AI](https://www.resemble.ai/) deepfake-detection API to
see which **oldcam-vN** variant (or the original Kling render) best survives a
synthetic-media detector.

A lower deepfake score means the detector was more convinced the clip is real,
so **lower is better** — the lowest-scoring video is flagged as the winner.

This is a self-contained standalone subproject (it mirrors `similarity/`). It
does not modify or depend on the main app at runtime.

## Quick start

### Windows

```bat
resemble-score\run_gui.bat      :: graphical app
resemble-score\run_cli.bat      :: terminal app
```

### macOS

```bash
resemble-score/run_gui.command  # graphical app
resemble-score/run_cli.command  # terminal app
```

The launchers create/reuse a Python environment and install
`requirements.txt` automatically. No manual setup needed.

## API key

`resemble-score` looks for `RESEMBLE_API_KEY` in this order:

1. `resemble-score/.env` (create it — it is gitignored and never committed)
2. external `.env` path(s): the `RESEMBLE_EXTERNAL_ENV` env var
   (`os.pathsep`-separated) if set, otherwise the built-in best-effort
   defaults `C:\claude\Resemble\resemble\.env` /
   `F:\claude\Resemble\resemble\.env` — each read only if it exists, so
   absence is harmless on any machine
3. the `RESEMBLE_API_KEY` environment variable

`.env` format:

```dotenv
RESEMBLE_API_KEY=your-key-here
```

If no key is found the app prints a clear message and exits — it never crashes
with a stack trace for a missing key.

## What it does

1. **Pick a folder.** Optionally recurse into subfolders.
2. **Discovery.** Every video is classified by filename:
   - `*-oldcam-vN.<ext>` → **Oldcam vN**
   - any other video → **Original/Kling**
3. **Select.** Choose exactly which videos to score (checkboxes in the GUI;
   numbered multi-select in the CLI — `1,3`, `g:oldcam`, `g:original`, `all`).
4. **Score.** Videos are submitted one at a time on a background worker so the
   UI stays responsive, with live `N of M` progress.
5. **Compare.** A ranked table (lowest score = winner, highlighted) plus:
   - `<video>.json` next to each scored video (trimmed Resemble result)
   - `resemble_results.json` + `resemble_results.csv` in the scanned folder

## CLI examples

```bash
python resemble-score/main.py --cli --folder "F:\videos" --recursive
python resemble-score/main.py --cli --folder "F:\videos" --all
python resemble-score/main.py --cli --folder "F:\videos" --select "g:oldcam"
```

Run with no arguments to launch the GUI.
