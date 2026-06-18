"""Reusable post-processing MODE-COMPARISON harness.

Runs any source video through any selection of post-processing modes
(oldcam vN / AA prime|scenario1|scenario3 / rPPG / crush), keeps every variant
on disk, scores each with the local no-API temporal-cadence forensics model,
and emits a SELF-CONTAINED interactive HTML report with draggable before/after
sliders (original vs each mode), mid-point frames, a metrics table, and the
per-mode processing time.

NOT a pytest test — it shells the real (slow, sometimes GPU) generators and
needs ffmpeg + the subprojects (aa-video/, rPPG/, oldcam-v*/). For the dev box.

Usage
-----
    venv/Scripts/python.exe oldcam-testing/mode_compare.py --source <video.mp4> \
        [--modes oldcam:v13,oldcam:v24,aa:prime,aa:scenario1,aa:scenario3,rppg,crush:720p] \
        [--out <dir>] [--strength 0.5] [--generator kling] \
        [--skip-existing] [--no-html] [--open]

Defaults: --modes oldcam:v13,oldcam:v24,aa:prime,aa:scenario1,aa:scenario3,rppg
          --out oldcam-testing/mode_compare_out
Mode syntax: "oldcam:vN", "aa:prime|scenario1|scenario3", "rppg", "crush:720p|480p".
--skip-existing reuses any variant .mp4 already in --out (fast re-runs / add a mode).

All outputs are gitignored (under oldcam-testing/mode_compare_out/ by default).
"""
from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "resemble-score"))

DEFAULT_MODES = "oldcam:v13,oldcam:v24,aa:prime,aa:scenario1,aa:scenario3,rppg"


# --------------------------------------------------------------------------
# Mode runners — each returns (output_path | None, elapsed_seconds).
# Imports are local so a missing subproject only breaks its own mode.
# --------------------------------------------------------------------------
def _run_oldcam(src: Path, version: str, out_dir: Path, _strength, _gen, log) -> Optional[Path]:
    from automation.oldcam import run_oldcam_version
    r = run_oldcam_version(video_path=src, version=version, repo_root=REPO_ROOT, progress_cb=log)
    if not r:
        return None
    dst = out_dir / f"oldcam_{version}{src.suffix}"
    shutil.copy2(r, dst)
    return dst


def _run_aa(src: Path, attack: str, out_dir: Path, strength: float, gen: str, log) -> Optional[Path]:
    from automation.video_aa import run_aa
    dst = out_dir / f"aa_{attack}{src.suffix}"
    r = run_aa(str(src), output_path=str(dst), attack=attack, strength=strength,
               generator=gen or None, log_callback=log, repo_root=REPO_ROOT)
    return Path(r) if r else None


def _run_rppg(src: Path, _arg, out_dir: Path, _strength, _gen, log) -> Optional[Path]:
    from automation.rppg import run_rppg, resolve_rppg_launcher
    if resolve_rppg_launcher(REPO_ROOT) is None:
        log("rPPG launcher missing", "warning")
        return None
    # run_rppg writes next to the input + renames with a metrics suffix; feed a
    # dedicated copy then capture the produced path.
    rppg_in = out_dir / f"_rppg_src{src.suffix}"
    shutil.copy2(src, rppg_in)
    r = run_rppg(video_path=rppg_in, repo_root=REPO_ROOT, progress_cb=log)
    try:
        rppg_in.unlink(missing_ok=True)
    except OSError:
        pass
    if not (r and Path(r).exists()):
        return None
    dst = out_dir / f"rppg{src.suffix}"
    shutil.copy2(r, dst)
    return dst


def _run_crush(src: Path, tier: str, out_dir: Path, _strength, _gen, log) -> Optional[Path]:
    from automation.video_crush import crush_video, CRUSH_RESOLUTIONS
    height = CRUSH_RESOLUTIONS.get(tier)
    if not height:
        log(f"unknown crush tier {tier}", "warning")
        return None
    dst = out_dir / f"crush_{tier}{src.suffix}"
    r = crush_video(str(src), output_path=str(dst), target_height=height, log_callback=log)
    return Path(r) if r else None


_RUNNERS: Dict[str, Callable] = {
    "oldcam": _run_oldcam, "aa": _run_aa, "rppg": _run_rppg, "crush": _run_crush,
}


def parse_modes(spec: str) -> List[Tuple[str, str, str]]:
    """'oldcam:v13,aa:prime,rppg' -> [(label, kind, arg), ...]."""
    out = []
    for tok in (t.strip() for t in spec.split(",") if t.strip()):
        kind, _, arg = tok.partition(":")
        kind = kind.lower()
        if kind not in _RUNNERS:
            raise SystemExit(f"unknown mode '{kind}' in '{tok}' "
                             f"(valid: {', '.join(_RUNNERS)})")
        label = f"{kind}_{arg}" if arg else kind
        out.append((label, kind, arg))
    return out


# --------------------------------------------------------------------------
# HTML report
# --------------------------------------------------------------------------
def _midpoint_frame_b64(path: Path) -> Optional[str]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30)
        t = max(0.1, float(r.stdout.strip()) / 2.0)
    except Exception:
        t = 1.0
    png = path.with_name(path.stem + "_mid.png")
    subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(path),
                    "-frames:v", "1", "-q:v", "2", str(png)],
                   capture_output=True, timeout=60)
    if not png.exists():
        return None
    return base64.b64encode(png.read_bytes()).decode("ascii")


def build_html(source: Path, results: List[dict], out_dir: Path) -> Path:
    # results: [{label, display, path, metrics(dict|None), seconds(float|None)}]
    frames = {}
    for r in results:
        if r["path"] and Path(r["path"]).exists():
            b = _midpoint_frame_b64(Path(r["path"]))
            if b:
                frames[r["label"]] = b
    orig = next((r for r in results if r["label"] == "original"), None)
    orig_b64 = frames.get("original", "")

    cards = []
    for r in results:
        if r["label"] == "original" or r["label"] not in frames:
            continue
        m, om = r.get("metrics"), (orig or {}).get("metrics")
        delta = ""
        if m and om:
            dc = m["composite"] - om["composite"]
            delta = (f"composite {om['composite']:.2f} → {m['composite']:.2f} "
                     f"({'+' if dc >= 0 else ''}{dc:.2f}) · {m['verdict']}")
        if r.get("seconds") is not None:
            delta += f" · ⏱ {r['seconds']:.0f}s"
        cards.append(f"""
    <div class="card"><h2>Kling original <span class="vs">vs</span> {r['display']}</h2>
      <div class="slider">
        <img class="after" src="data:image/png;base64,{frames[r['label']]}">
        <div class="before-wrap"><img class="before" src="data:image/png;base64,{orig_b64}"></div>
        <div class="handle"></div>
        <span class="lbl lbl-l">original</span><span class="lbl lbl-r">{r['display']}</span>
      </div><p class="delta">{delta}</p></div>""")

    def trow(r):
        m = r.get("metrics")
        if not m:
            return ""
        s = r.get("seconds")
        sd = f"{s:.0f}s" if s is not None else ("—" if r["label"] == "original" else "?")
        return (f"<tr><td>{r['display']}</td><td>{m['composite']:.3f}</td><td>{m['verdict']}</td>"
                f"<td>{m['smoothness']:.3f}</td><td>{m['burst_pct']:.1f}</td>"
                f"<td>{m['freeze_pct']:.1f}</td><td>{m['jerk']:.3f}</td><td>{sd}</td></tr>")
    rows = "".join(trow(r) for r in results)

    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mode comparison — {source.name}</title><style>
:root{{--bg:#15161a;--card:#1e2026;--bd:#33363f;--txt:#e6e6e6;--dim:#9aa0aa;--accent:#5b9cff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:28px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--dim);margin:0 0 24px;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:22px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px}}
.card h2{{font-size:15px;margin:0 0 12px;font-weight:600}}.vs{{color:var(--accent);font-weight:700;padding:0 4px}}
.slider{{position:relative;width:100%;aspect-ratio:3/4;overflow:hidden;border-radius:8px;
user-select:none;cursor:ew-resize;background:#000}}
.slider img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;pointer-events:none}}
.before-wrap{{position:absolute;inset:0;height:100%;width:50%;overflow:hidden;border-right:2px solid var(--accent)}}
.before-wrap img{{width:100vw;max-width:none}}
.handle{{position:absolute;top:0;left:50%;width:2px;height:100%;background:var(--accent);transform:translateX(-1px);pointer-events:none}}
.handle::after{{content:'⇄';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
background:var(--accent);color:#000;font-size:13px;width:26px;height:26px;border-radius:50%;
display:flex;align-items:center;justify-content:center}}
.lbl{{position:absolute;bottom:8px;font-size:11px;padding:2px 7px;border-radius:4px;background:rgba(0,0,0,.65);color:#fff}}
.lbl-l{{left:8px}}.lbl-r{{right:8px}}.delta{{color:var(--dim);font-size:12px;margin:10px 2px 0;font-family:ui-monospace,monospace}}
table{{border-collapse:collapse;width:100%;margin-top:34px;font-size:13px}}
th,td{{border:1px solid var(--bd);padding:7px 10px;text-align:right}}
th:first-child,td:first-child{{text-align:left}}th{{background:#23262e;color:var(--dim)}}
.note{{color:var(--dim);font-size:12px;margin-top:14px;max-width:760px}}</style></head><body>
<h1>Post-processing mode comparison</h1>
<p class="sub">Source: {source.name} · mid-point frame · drag the divider to reveal original vs each mode.</p>
<div class="grid">{''.join(cards)}</div>
<h2 style="margin-top:38px">Temporal-cadence heuristics (local, no-API) + process time</h2>
<table><thead><tr><th>Mode</th><th>composite</th><th>verdict</th><th>smoothness</th>
<th>burst %</th><th>freeze %</th><th>jerk</th><th>time</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">Lower <b>composite</b> = smoother cadence (spatial-only attacks like oldcam/crush win on smooth
clips; the temporal tell survives spatial processing). n=1 clip, local heuristic — NOT the live Resemble API.
Per resemble-score/FORENSICS.md.</p></body>
<script>
document.querySelectorAll('.slider').forEach(function(s){{
 var bw=s.querySelector('.before-wrap'),h=s.querySelector('.handle');
 function set(x){{var r=s.getBoundingClientRect();var p=Math.max(0,Math.min(1,(x-r.left)/r.width));
  bw.style.width=(p*100)+'%';h.style.left=(p*100)+'%';}}
 var d=false;s.addEventListener('mousedown',function(e){{d=true;set(e.clientX);}});
 window.addEventListener('mousemove',function(e){{if(d)set(e.clientX);}});
 window.addEventListener('mouseup',function(){{d=false;}});
 s.addEventListener('touchstart',function(e){{d=true;set(e.touches[0].clientX);}},{{passive:true}});
 s.addEventListener('touchmove',function(e){{if(d)set(e.touches[0].clientX);}},{{passive:true}});
 s.addEventListener('touchend',function(){{d=false;}});}});
</script></html>"""
    report = out_dir / "mode_comparison.html"
    report.write_text(html, encoding="utf-8")
    return report


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Reusable post-processing mode comparison harness.")
    ap.add_argument("--source", required=True, help="source video (a Kling original)")
    ap.add_argument("--modes", default=DEFAULT_MODES,
                    help=f"comma list of modes (default: {DEFAULT_MODES})")
    ap.add_argument("--out", default=str(REPO_ROOT / "oldcam-testing" / "mode_compare_out"),
                    help="output dir (gitignored)")
    ap.add_argument("--strength", type=float, default=0.5, help="AA strength 0.1-1.0")
    ap.add_argument("--generator", default="kling", help="AA generator profile")
    ap.add_argument("--skip-existing", action="store_true",
                    help="reuse variant .mp4s already in --out")
    ap.add_argument("--no-html", action="store_true", help="skip the HTML report")
    ap.add_argument("--open", action="store_true", help="open the report when done")
    args = ap.parse_args()

    src = Path(args.source).expanduser().resolve()
    if not src.is_file():
        raise SystemExit(f"source not found: {src}")
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    modes = parse_modes(args.modes)

    def log(msg, level="info"):
        if level in ("warning", "error"):
            print(f"  ({level}) {str(msg)[:110]}", flush=True)

    def stamp(m):
        print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)

    # forensics import (cv2/numpy — main venv).
    from src.forensics import analyze_clip

    results: List[dict] = []

    orig = out_dir / f"original{src.suffix}"
    if not orig.exists():
        shutil.copy2(src, orig)
    results.append({"label": "original", "display": "Kling original",
                    "path": orig, "seconds": None})

    for label, kind, arg in modes:
        dst_guess = out_dir / f"{label}{src.suffix}"
        display = label.replace("_", " ").replace("aa ", "AA ").replace("oldcam ", "Oldcam ").replace("rppg", "rPPG").replace("crush ", "Crush ")
        if args.skip_existing and dst_guess.exists():
            stamp(f"{label}: reusing existing")
            results.append({"label": label, "display": display, "path": dst_guess, "seconds": None})
            continue
        stamp(f"{label}: running…")
        t0 = time.time()
        try:
            produced = _RUNNERS[kind](src, arg, out_dir, args.strength, args.generator, log)
        except Exception as exc:  # one mode failing must not kill the rest
            stamp(f"  {label} ERROR: {exc}")
            produced = None
        dt = time.time() - t0
        if produced and Path(produced).exists():
            stamp(f"  {label} -> {Path(produced).name} ({dt:.0f}s)")
            results.append({"label": label, "display": display, "path": Path(produced), "seconds": dt})
        else:
            stamp(f"  {label} produced nothing ({dt:.0f}s)")
            results.append({"label": label, "display": display, "path": None, "seconds": dt})

    # score
    stamp("scoring…")
    for r in results:
        if r["path"] and Path(r["path"]).exists():
            try:
                f = analyze_clip(str(r["path"]))
                r["metrics"] = f.to_dict() if f else None
            except Exception as exc:
                stamp(f"  score {r['label']} failed: {exc}")
                r["metrics"] = None
        else:
            r["metrics"] = None

    # table to stdout
    stamp("=" * 60)
    for r in results:
        m = r.get("metrics")
        if m:
            s = f"{r['seconds']:.0f}s" if r.get("seconds") is not None else "—"
            stamp(f"{r['label']:16s} composite={m['composite']:6.3f} {m['verdict']:10s} "
                  f"freeze={m['freeze_pct']:.1f}% jerk={m['jerk']:.3f} {s}")

    if not args.no_html:
        report = build_html(src, results, out_dir)
        stamp(f"HTML: {report}")
        if args.open:
            webbrowser.open(report.as_uri())
    stamp("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
