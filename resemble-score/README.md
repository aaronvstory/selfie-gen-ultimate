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
5. **Compare.** A ranked table (lowest score = winner, highlighted) plus
   these files written into the scanned folder:
   - `<video>.json` next to each scored video (trimmed Resemble result)
   - `resemble_results.json` — combined ranked data
   - `resemble_results.csv` — ranked table for spreadsheets
   - `resemble_results.md` — human-readable report: winner callout, run
     metadata, and a ranked Markdown table (paste-ready into notes/PRs)

## CLI examples

```bash
python resemble-score/main.py --cli --folder "F:\videos" --recursive
python resemble-score/main.py --cli --folder "F:\videos" --all
python resemble-score/main.py --cli --folder "F:\videos" --select "g:oldcam"
```

Run with no arguments to launch the GUI.

## Temporal forensics — free pre-test (no API cost)

```bash
python resemble-score/main.py --forensics --folder "F:\videos"
```

Before spending a Resemble API call, this offline pass measures each
clip's **motion cadence** and predicts whether oldcam (V24) can help:

- **`spatial`** — smooth cadence; the deepfake tell (if any) is the
  spatial diffusion fingerprint, which V24's resolution crush destroys.
  *Worth scoring — expect a large improvement.*
- **`temporal`** — broken motion rhythm (bursts of fast motion + frozen
  frames + jerky acceleration). The tell is temporal; V24 only alters
  pixels, so it **cannot** help. *Re-generate the source instead of
  spending an API call.*
- **`uncertain`** — calibration grey zone; score it to find out.

Discovered empirically from 3 known-outcome clips and calibrated against
a 181-clip real corpus (see `FORENSICS.md`). On that corpus ~65% read
"spatial" (V24 candidates), ~4% "temporal" (don't bother), the rest
"uncertain". The Signal-relayed `signal-…` clip — V24's only total
failure — sat at composite 4.97, far above the corpus max of 2.16,
confirming it as a genuine extreme outlier.
