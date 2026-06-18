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
import html as _html
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
def _video_dims(path: Path) -> Optional[tuple]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            capture_output=True, text=True, timeout=30)
        w, h = r.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return None


def _human_size(nbytes: int) -> str:
    f = float(nbytes)
    for unit in ("B", "KB", "MB"):
        if f < 1024:
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def _file_meta(path: Path) -> str:
    """One-line 'name · WxH · size' for a video file (its NATIVE resolution)."""
    try:
        name = path.name
        size = _human_size(path.stat().st_size)
        dims = _video_dims(path)
        res = f"{dims[0]}×{dims[1]}" if dims else "?"
        return f"{name} · {res} · {size}"
    except Exception:
        return path.name


def _midpoint_frame_b64(path: Path, target_size: Optional[tuple] = None) -> Optional[str]:
    """Extract the mid-point frame. When target_size=(W,H) is given, scale the
    frame to those exact dims so every variant ends up the same pixel
    dimensions and the slider/lightbox overlay is a true 1:1 spatial
    comparison. Uses the DEFAULT (bicubic) scaler — i.e. how a video player
    actually upscales a lower-res clip (e.g. 480p crush) — so the frame looks
    the way it really would on screen (soft/low-detail), not artificially
    blocky. We compare the videos as they ARE, not a synthetic pixel grid.
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, errors="replace", timeout=30)
        t = max(0.1, float(r.stdout.strip()) / 2.0)
    except Exception:
        t = 1.0
    png = path.with_name(path.stem + "_mid.png")
    # Delete any prior frame FIRST so a failed/timed-out ffmpeg can't silently
    # reuse a stale frame from an earlier run (CodeRabbit). Only accept the
    # output when ffmpeg actually returns 0 and writes the file.
    try:
        png.unlink(missing_ok=True)
    except OSError:
        pass
    cmd = ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1"]
    if target_size:
        w, h = target_size
        cmd += ["-vf", f"scale={w}:{h}"]  # default (bicubic) — true on-screen look
    cmd += ["-q:v", "2", str(png)]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None
    if cp.returncode != 0 or not png.exists():
        return None
    return base64.b64encode(png.read_bytes()).decode("ascii")


def build_html(source: Path, results: List[dict], out_dir: Path) -> Path:
    # results: [{label, display, path, metrics(dict|None), seconds(float|None)}]
    # Normalize every extracted frame to the SOURCE resolution so a lower-res
    # variant (480p/720p crush) is upscaled to the original's size and overlays
    # 1:1 — a real pixel-for-pixel comparison, not a size mismatch.
    target = _video_dims(source)
    # Aspect ratio from the actual source (fall back to 3/4) so the viewers
    # match the clip and don't crop/letterbox a non-3:4 video (CodeRabbit).
    ar = f"{target[0]}/{target[1]}" if target else "3/4"
    frames = {}
    for r in results:
        if r["path"] and Path(r["path"]).exists():
            b = _midpoint_frame_b64(Path(r["path"]), target_size=target)
            if b:
                frames[r["label"]] = b
    orig = next((r for r in results if r["label"] == "original"), None)
    orig_b64 = frames.get("original", "")
    orig_meta = _file_meta(Path(orig["path"])) if orig and orig.get("path") else "original"
    orig_meta_e = _html.escape(orig_meta, quote=True)

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
        # Escape everything filename-derived before it goes into HTML
        # attributes/text — a quote or < in a clip name would otherwise break
        # the data-* attrs or inject markup (CodeRabbit).
        disp = _html.escape(str(r["display"]), quote=True)
        var_meta_e = _html.escape(_file_meta(Path(r["path"])), quote=True)
        cards.append(f"""
    <div class="card">
      <h2>Kling original <span class="vs">vs</span> {disp}
        <button class="zoom-btn" data-label="{disp}"
          data-after="data:image/png;base64,{frames[r['label']]}"
          data-before="data:image/png;base64,{orig_b64}"
          data-ameta="{orig_meta_e}" data-bmeta="{var_meta_e}">⛶ zoom</button></h2>
      <div class="slider" style="--ar:{ar};--split:50%">
        <img class="after" src="data:image/png;base64,{frames[r['label']]}">
        <img class="before" src="data:image/png;base64,{orig_b64}">
        <div class="handle"></div>
        <span class="lbl lbl-l">original</span><span class="lbl lbl-r">{disp}</span>
      </div>
      <p class="delta">{delta}</p>
      <p class="meta"><span class="meta-o">◀ {orig_meta_e}</span><span class="meta-v">{var_meta_e} ▶</span></p>
    </div>""")

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
.slider{{position:relative;width:100%;aspect-ratio:var(--ar,3/4);overflow:hidden;border-radius:8px;
user-select:none;cursor:ew-resize;background:#000}}
/* Both images are IDENTICAL full-size overlays of the slider — same width,
   height, object-fit, position — so they line up pixel-for-pixel. The
   "before" (original) is clipped to the left of the --split line via
   clip-path; nothing is rescaled, so dragging is a true wipe comparison.
   object-fit:contain (not cover) + matching aspect-ratio means no edge pixels
   are cropped, so the comparison covers the whole frame. */
.slider img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;pointer-events:none}}
.slider .before{{clip-path:inset(0 calc(100% - var(--split)) 0 0);
border-right:2px solid var(--accent)}}
.handle{{position:absolute;top:0;left:var(--split);width:2px;height:100%;background:var(--accent);transform:translateX(-1px);pointer-events:none}}
.handle::after{{content:'⇄';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
background:var(--accent);color:#000;font-size:13px;width:26px;height:26px;border-radius:50%;
display:flex;align-items:center;justify-content:center}}
.lbl{{position:absolute;bottom:8px;font-size:11px;padding:2px 7px;border-radius:4px;background:rgba(0,0,0,.65);color:#fff}}
.lbl-l{{left:8px}}.lbl-r{{right:8px}}.delta{{color:var(--dim);font-size:12px;margin:10px 2px 0;font-family:ui-monospace,monospace}}
.meta{{display:flex;justify-content:space-between;gap:8px;color:var(--dim);font-size:10.5px;
margin:5px 2px 0;font-family:ui-monospace,monospace}}
.meta-o{{text-align:left}}.meta-v{{text-align:right}}
.zoom-btn{{float:right;font:600 11px/1 -apple-system,Segoe UI,sans-serif;color:var(--txt);
background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:4px 8px;cursor:pointer}}
.zoom-btn:hover{{background:var(--accent);color:#000;border-color:var(--accent)}}
/* Lightbox: full-screen enlarged compare. Two views toggled by the buttons:
   a big draggable slider, and a true side-by-side (both full frames). */
#lb{{position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:99;display:none;
flex-direction:column;align-items:center;justify-content:center;padding:18px}}
#lb.open{{display:flex}}
#lb .bar{{position:absolute;top:12px;left:0;right:0;display:flex;gap:10px;
align-items:center;justify-content:center;color:var(--txt);font-size:14px}}
#lb .bar button{{font:600 13px/1 -apple-system,Segoe UI,sans-serif;color:var(--txt);
background:var(--card);border:1px solid var(--bd);border-radius:6px;padding:7px 13px;cursor:pointer}}
#lb .bar button.on{{background:var(--accent);color:#000;border-color:var(--accent)}}
#lb .stage{{max-width:96vw;max-height:82vh;display:flex;align-items:center;justify-content:center}}
/* ZOOMABLE slider: the .lbslider is the fixed viewport (clips). Inside it,
   .zoomwrap holds both full images and is scaled+panned together via
   transform, so the WIPE (clip-path on .before) and the ZOOM/PAN stay in
   sync — you can drag the divider AND be zoomed in at the same time. */
#lb .lbslider{{position:relative;height:82vh;aspect-ratio:var(--ar,3/4);overflow:hidden;border-radius:8px;
user-select:none;cursor:ew-resize;background:#000;--split:50%}}
/* Two stacked LAYERS, each clipped in the slider's FIXED viewport space. The
   "before" layer is clipped to the left of --split (a viewport %) so the wipe
   is always correct REGARDLESS of zoom. Inside each layer a .zoomwrap holds
   the image and gets the SAME transform, so both images pan/zoom together. */
#lb .layer{{position:absolute;inset:0;overflow:hidden}}
#lb .layer.before{{clip-path:inset(0 calc(100% - var(--split)) 0 0);border-right:2px solid var(--accent)}}
#lb .zoomwrap{{position:absolute;inset:0;transform-origin:0 0;
transform:translate(var(--px,0px),var(--py,0px)) scale(var(--zoom,1))}}
#lb .zoomwrap img{{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;pointer-events:none}}
#lb .lbslider .h{{position:absolute;top:0;left:var(--split);width:2px;height:100%;
background:var(--accent);transform:translateX(-1px);pointer-events:none;z-index:2}}
#lb .lbslider .h::after{{content:'⇄';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
background:var(--accent);color:#000;font-size:14px;width:30px;height:30px;border-radius:50%;
display:flex;align-items:center;justify-content:center}}
#lb .lbl{{z-index:2}}
#lb .footer{{position:absolute;bottom:10px;left:0;right:0;display:flex;justify-content:center;
gap:26px;color:var(--dim);font:12px/1.3 ui-monospace,monospace}}
#lb .footer b{{color:var(--txt);font-weight:600}}
#lb .zhint{{position:absolute;bottom:34px;left:0;right:0;text-align:center;color:var(--dim);font-size:11px}}
#lb .close{{position:absolute;top:12px;right:16px;font-size:26px;color:var(--txt);cursor:pointer;background:none;border:none;z-index:3}}
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
Per resemble-score/FORENSICS.md.</p>

<div id="lb">
  <div class="bar">
    <span id="lb-title"></span>
    <button id="lb-zoomout">−</button><span id="lb-zlvl">100%</span><button id="lb-zoomin">+</button>
    <button id="lb-reset">Fit</button>
  </div>
  <button class="close" id="lb-close">✕</button>
  <div class="stage">
    <div class="lbslider" id="lbsl" style="--ar:{ar};--split:50%">
      <div class="layer after"><div class="zoomwrap"><img id="lb-after"></div></div>
      <div class="layer before"><div class="zoomwrap"><img id="lb-before"></div></div>
      <div class="h"></div>
      <span class="lbl lbl-l">original</span><span class="lbl lbl-r" id="lb-rlbl"></span>
    </div>
  </div>
  <div class="zhint">grab the ⇄ divider = wipe · drag elsewhere = pan (when zoomed) · scroll / +− = zoom · Fit resets</div>
  <div class="footer">
    <span><b>◀ original</b> · <span id="lb-ameta"></span></span>
    <span><span id="lb-bmeta"></span> · <b id="lb-bname">mode ▶</b></span>
  </div>
</div>
</body>
<script>
// Card sliders (clip-path overlay; both images are identical full-size layers).
document.querySelectorAll('.slider').forEach(function(s){{
 function set(x){{var r=s.getBoundingClientRect();var p=Math.max(0,Math.min(1,(x-r.left)/r.width));
  s.style.setProperty('--split',(p*100)+'%');}}
 var d=false;s.addEventListener('mousedown',function(e){{d=true;set(e.clientX);}});
 window.addEventListener('mousemove',function(e){{if(d)set(e.clientX);}});
 window.addEventListener('mouseup',function(){{d=false;}});
 s.addEventListener('touchstart',function(e){{d=true;set(e.touches[0].clientX);}},{{passive:true}});
 s.addEventListener('touchmove',function(e){{if(d)set(e.touches[0].clientX);}},{{passive:true}});
 s.addEventListener('touchend',function(){{d=false;}});}});

// Lightbox: ONE zoomable slider. Wipe (clip-path --split) + zoom/pan
// (transform on .zoomwrap) stay in sync, so you can be zoomed in AND wipe.
var lb=document.getElementById('lb'),lbsl=document.getElementById('lbsl'),
 zlvl=document.getElementById('lb-zlvl');
var zoom=1, px=0, py=0;
function applyZoom(){{
 // Set the transform vars on the SLIDER; both .zoomwrap layers inherit them,
 // so the after + before images pan/zoom identically while the before stays
 // clipped in viewport space (correct wipe at any zoom).
 lbsl.style.setProperty('--zoom',zoom); lbsl.style.setProperty('--px',px+'px');
 lbsl.style.setProperty('--py',py+'px'); zlvl.textContent=Math.round(zoom*100)+'%';
 lbsl.style.cursor = zoom>1 ? 'move' : 'ew-resize';
}}
function resetZoom(){{zoom=1;px=0;py=0;applyZoom();}}
function setZoom(z,cx,cy){{ // zoom toward viewport point (cx,cy) relative to slider
 var r=lbsl.getBoundingClientRect();
 var ox=(cx-r.left-px)/zoom, oy=(cy-r.top-py)/zoom;
 zoom=Math.max(1,Math.min(8,z));
 px=cx-r.left-ox*zoom; py=cy-r.top-oy*zoom;
 // clamp pan so the image edges don't drift inside the frame
 var maxX=0,maxY=0,minX=r.width-r.width*zoom,minY=r.height-r.height*zoom;
 px=Math.max(minX,Math.min(maxX,px)); py=Math.max(minY,Math.min(maxY,py));
 applyZoom();
}}
document.querySelectorAll('.zoom-btn').forEach(function(btn){{
 btn.addEventListener('click',function(){{
  document.getElementById('lb-after').src=btn.dataset.after;
  document.getElementById('lb-before').src=btn.dataset.before;
  document.getElementById('lb-rlbl').textContent=btn.dataset.label;
  document.getElementById('lb-title').textContent='original  vs  '+btn.dataset.label;
  document.getElementById('lb-ameta').textContent=btn.dataset.ameta||'';
  document.getElementById('lb-bmeta').textContent=btn.dataset.bmeta||'';
  document.getElementById('lb-bname').textContent=btn.dataset.label+' ▶';
  lbsl.style.setProperty('--split','50%'); resetZoom();
  lb.classList.add('open');
 }});
}});
document.getElementById('lb-zoomin').onclick=function(){{var r=lbsl.getBoundingClientRect();setZoom(zoom*1.4,r.left+r.width/2,r.top+r.height/2);}};
document.getElementById('lb-zoomout').onclick=function(){{var r=lbsl.getBoundingClientRect();setZoom(zoom/1.4,r.left+r.width/2,r.top+r.height/2);}};
document.getElementById('lb-reset').onclick=resetZoom;
// scroll to zoom toward cursor
lbsl.addEventListener('wheel',function(e){{e.preventDefault();
 setZoom(zoom*(e.deltaY<0?1.2:1/1.2),e.clientX,e.clientY);}},{{passive:false}});
// Drag routing so you can do BOTH at any zoom:
//   • grab ON/near the divider line  -> WIPE (move the split)
//   • drag anywhere else             -> PAN (when zoomed) / WIPE (when 1x)
// A ~22px grab band around the divider makes it easy to catch.
(function(){{var mode=null,sx=0,sy=0,spx=0,spy=0;
 function wipe(x){{var r=lbsl.getBoundingClientRect();var p=Math.max(0,Math.min(1,(x-r.left)/r.width));
  lbsl.style.setProperty('--split',(p*100)+'%');}}
 function splitX(){{var r=lbsl.getBoundingClientRect();
  return r.left + r.width * (parseFloat(lbsl.style.getPropertyValue('--split'))||50)/100;}}
 lbsl.addEventListener('mousedown',function(e){{sx=e.clientX;sy=e.clientY;spx=px;spy=py;
  var nearDivider = Math.abs(e.clientX - splitX()) <= 22;
  if(nearDivider || zoom<=1){{mode='wipe';wipe(e.clientX);}} else {{mode='pan';}}
  e.preventDefault();}});
 window.addEventListener('mousemove',function(e){{if(!mode)return;
  if(mode==='pan'){{px=spx+(e.clientX-sx);py=spy+(e.clientY-sy);
   var r=lbsl.getBoundingClientRect();
   px=Math.max(r.width-r.width*zoom,Math.min(0,px));py=Math.max(r.height-r.height*zoom,Math.min(0,py));
   applyZoom();}} else {{wipe(e.clientX);}} }});
 window.addEventListener('mouseup',function(){{mode=null;}});}})();
function lbClose(){{lb.classList.remove('open');}}
document.getElementById('lb-close').onclick=lbClose;
lb.addEventListener('click',function(e){{if(e.target===lb)lbClose();}});
document.addEventListener('keydown',function(e){{if(e.key==='Escape')lbClose();}});
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
    ap.add_argument("--out", default=None,
                    help="output dir. Default: test-material/mode-comparisons/"
                         "<YYYY-MM-DD>_<source-stem>/ — a permanent, dated, "
                         "self-describing folder holding the variant videos, "
                         "mid-point frames, and the HTML report together.")
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
    if args.out:
        out_dir = Path(args.out).expanduser().resolve()
    else:
        # Permanent, dated, self-describing home so runs are never treated as
        # throwaway scratch: test-material/mode-comparisons/<date>_<source-stem>/.
        # Same date+source reuses the same folder (pairs with --skip-existing).
        day = datetime.now().strftime("%Y-%m-%d")
        out_dir = (REPO_ROOT / "test-material" / "mode-comparisons"
                   / f"{day}_{src.stem}")
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
