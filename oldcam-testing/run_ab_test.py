#!/usr/bin/env python3
"""
run_ab_test.py — produce an oldcam vN clip, score ONLY it, compare to the
existing scored corpus, and emit a standalone HTML report.

Standalone experiment harness (not wired into the app). Version-agnostic:
pass --version vN and it runs oldcam-testing/oldcam_vN.py. The expensive
part is the Resemble API call, so this is deliberately frugal:

  1. Run the source clip through oldcam_vN.py, producing
     `<stem>-oldcam-vN.mp4` IN the corpus folder (next to the
     already-scored original + v7..v15).
  2. Score ONLY the new vN file via one Resemble API call
     (resemble-score's client.detect_video) and write its
     `<vN>.mp4.json` sidecar — same shape resemble-score writes.
  3. Load every OTHER clip's already-written sidecar from disk
     (resemble-score's scoring.load_existing_result — NO API, no cost) so
     the original Kling render and v7..v15 are reused, never re-scored.
  4. Rank everything with resemble-score's own `rank()` (frame_mean
     ascending, lower = more authentic) and emit:
       - oldcam-testing/reports/<vN>_report_<ts>.html  (rich, openable)
       - a ranked block appended to oldcam-testing/SCOREBOARD.md

The comparison highlights the tested vN vs the good V15
(`-oldcam-v15.mp4`, not the bad `-v15-v1`). Corpus defaults to the GISELLE
gen-images folder; override with --corpus. Requires RESEMBLE_API_KEY
discoverable by resemble-score and ffmpeg on PATH.

Usage:
    python oldcam-testing/run_ab_test.py                  # v17 (default)
    python oldcam-testing/run_ab_test.py --version v16
    python oldcam-testing/run_ab_test.py --source "F:/x.mp4"
    python oldcam-testing/run_ab_test.py --no-score       # make only
    python oldcam-testing/run_ab_test.py --report-only    # rebuild HTML
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_RESEMBLE_DIR = _REPO_ROOT / "resemble-score"

# resemble-score is a sibling subproject. Import its proven modules so the
# score parsing / ranking is byte-identical to that tool's GUI/CLI.
for _p in (str(_REPO_ROOT), str(_RESEMBLE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

REPORTS_DIR = _HERE / "reports"
SCOREBOARD = _HERE / "SCOREBOARD.md"

# The team's standing test corpus (already holds scored original + v7..v15).
DEFAULT_CORPUS = Path(
    r"F:\Downloads\Telegram Desktop\DLs\versailles\organized"
    r"\APPLIED - GISELLE_MARIE-HALE-05191979\gen-images"
)
DEFAULT_SOURCE = DEFAULT_CORPUS / (
    "front_crop_nano-banana-2-edit_sim83_001_k25tStd_p4_1.mp4"
)
DEFAULT_VERSION = "v17"
_VERSION_RE = re.compile(r"^v\d+$")


def _rs():
    """Lazy import of resemble-score's modules (client/discovery/scoring)."""
    from src import client, discovery, scoring  # type: ignore
    return client, discovery, scoring


def _log(msg: str) -> None:
    print(msg, flush=True)


def version_script(version: str) -> Path:
    return _HERE / f"oldcam_{version}.py"


def make_clip(version: str, source: Path, corpus: Path) -> Path | None:
    """Run oldcam_<version> on `source`, output into the corpus folder."""
    script = version_script(version)
    if not script.is_file():
        _log(f"! {version} script missing: {script}")
        return None
    out = corpus / f"{source.stem}-oldcam-{version}{source.suffix}"
    _log(f"  $ python {script.name} {source.name} -o {out.name}")
    rc = subprocess.run(
        [sys.executable, str(script), str(source), "-o", str(out)],
        cwd=str(_REPO_ROOT),
    ).returncode
    if rc != 0 or not out.is_file():
        _log(f"! {version} oldcam run failed (rc={rc})")
        return None
    _log(f"  + produced {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


def score_only(clip_path: Path) -> Path:
    """One Resemble API call for the new clip; write its sidecar JSON."""
    client, _discovery, scoring = _rs()
    api_key = client.resolve_api_key()  # raises clean RuntimeError if absent
    _log(f"  Resemble: scoring {clip_path.name} (1 API call) ...")
    trimmed = client.detect_video(clip_path, api_key)
    sidecar = scoring.sidecar_json_path(clip_path)
    sidecar.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
    _log(f"  + wrote {sidecar.name}")
    return sidecar


def collect_results(corpus: Path):
    """Load every clip's existing sidecar (no API). Returns ranked Results."""
    _client, discovery, scoring = _rs()
    items = discovery.discover(corpus, recursive=False)
    results = []
    for it in items:
        r = scoring.load_existing_result(it.path, it.group)
        if r is not None:
            results.append(r)
        else:
            _log(f"  · no sidecar yet for {it.path.name} (skipped)")
    return scoring.rank(results)


def _fmt(v) -> str:
    return "—" if v is None else f"{v:.4f}"


def _conclusion(score) -> str:
    if score is None:
        return "—"
    if score < 0.3:
        return "Real"
    if score < 0.55:
        return "Neutral/Uncertain"
    return "Fake"


def _find(ranked, needle: str):
    return next((r for r in ranked if needle in r.name.lower()), None)


def write_html(ranked, corpus: Path, source: Path, version: str) -> Path:
    """Self-contained HTML report mirroring resemble-score's breakdown."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORTS_DIR / f"{version}_report_{stamp}.html"

    winner = next((r for r in ranked if r.ok), None)
    vN = _find(ranked, f"-oldcam-{version}.")
    # The "good" V15 is the plain -oldcam-v15.mp4, NOT the bad -v15-v1.
    v15 = next(
        (r for r in ranked
         if "-oldcam-v15." in r.name.lower()
         and "-v15-v1" not in r.name.lower()),
        None,
    )

    delta_html = ""
    if (vN and v15 and vN.frame_mean is not None
            and v15.frame_mean is not None):
        d = vN.frame_mean - v15.frame_mean
        better = d < 0
        delta_html = (
            f'<div class="delta {"good" if better else "bad"}">'
            f'{version.upper()} vs V15 (the good one): <b>'
            f'{"BEATS" if better else "LOSES TO"}</b> V15 by '
            f'{abs(d):.4f} frame-mean '
            f'({version.upper()} {_fmt(vN.frame_mean)} vs V15 '
            f'{_fmt(v15.frame_mean)}) — lower is better.</div>'
        )

    rows = []
    for r in ranked:
        is_vN = vN is not None and r.name == vN.name
        is_win = winner is not None and r.name == winner.name
        cls = []
        if is_win:
            cls.append("winner")
        if is_vN:
            cls.append("vN")
        rows.append(
            "<tr class='{cls}'>"
            "<td>{rank}</td><td class='name'>{name}{tag}</td>"
            "<td class='num'>{fm}</td><td class='num'>{fmin}</td>"
            "<td class='num'>{fmax}</td><td class='num'>{cm}</td>"
            "<td class='num'>{cert}</td><td>{verdict}</td>"
            "<td>{concl}</td><td class='num'>{fc}</td></tr>".format(
                cls=" ".join(cls),
                rank=("🏆 " if is_win else "")
                + (str(r.rank) if r.rank else "—"),
                name=html.escape(r.name),
                tag=" <span class='chip'>NEW</span>" if is_vN else "",
                fm=_fmt(r.frame_mean), fmin=_fmt(r.frame_min),
                fmax=_fmt(r.frame_max), cm=_fmt(r.chunk_mean),
                cert=_fmt(r.certainty),
                verdict=html.escape(r.verdict_label or "—"),
                concl=_conclusion(r.frame_mean),
                fc=r.frame_count or 0,
            )
        )

    vlabel = version.upper()
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oldcam {vlabel} Resemble A/B — {html.escape(source.name)}</title>
<style>
:root{{--bg:#0d1117;--panel:#161b22;--line:#30363d;--txt:#e6edf3;
--dim:#8b949e;--good:#3fb950;--bad:#f85149;--accent:#58a6ff;--win:#1f6feb33}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px}}
h1{{font-size:20px;margin:0 0 4px}}
.sub{{color:var(--dim);margin:0 0 20px;font-size:13px}}
.cards{{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px 18px;min-width:180px;flex:1}}
.card .k{{color:var(--dim);font-size:12px;text-transform:uppercase;
letter-spacing:.04em}}
.card .v{{font-size:24px;font-weight:700;margin-top:4px;word-break:break-all}}
.delta{{padding:12px 16px;border-radius:8px;margin:6px 0 22px;font-size:15px}}
.delta.good{{background:#3fb95022;border:1px solid var(--good)}}
.delta.bad{{background:#f8514922;border:1px solid var(--bad)}}
table{{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);
font-variant-numeric:tabular-nums}}
th{{background:#1c2330;color:var(--dim);font-size:12px;text-transform:uppercase;
letter-spacing:.04em}}
td.num{{text-align:right}}
td.name{{font-family:ui-monospace,Consolas,monospace;font-size:12px}}
tr.winner{{background:var(--win)}}
tr.vN td{{box-shadow:inset 3px 0 0 var(--accent)}}
.chip{{background:var(--accent);color:#0d1117;font-size:10px;font-weight:700;
padding:1px 6px;border-radius:999px;vertical-align:middle}}
.legend{{color:var(--dim);font-size:12px;margin:14px 0 0}}
code{{background:#1c2330;padding:1px 5px;border-radius:4px}}
</style></head><body><div class="wrap">
<h1>Oldcam {vlabel} — Resemble A/B</h1>
<p class="sub">Source: <code>{html.escape(source.name)}</code> &nbsp;·&nbsp;
corpus: <code>{html.escape(str(corpus))}</code> &nbsp;·&nbsp; {ts}<br>
Ranked by <b>frame&nbsp;mean</b> (Resemble per-frame deepfake probability,
0–1). <b>Lower = more authentic = better.</b> The top-level verdict rounds
to Fake for almost any AI clip, so the per-frame columns are the real
signal.</p>
<div class="cards">
<div class="card"><div class="k">Winner (lowest frame mean)</div>
<div class="v">{html.escape(winner.name) if winner else "—"}</div></div>
<div class="card"><div class="k">{vlabel} frame mean</div>
<div class="v">{_fmt(vN.frame_mean) if vN else "—"}</div></div>
<div class="card"><div class="k">V15 frame mean</div>
<div class="v">{_fmt(v15.frame_mean) if v15 else "—"}</div></div>
</div>
{delta_html}
<table><thead><tr><th>#</th><th>Video</th><th>Frame mean</th>
<th>Frame min</th><th>Frame max</th><th>Chunk mean</th><th>Certainty</th>
<th>Verdict (raw)</th><th>Conclusion</th><th>Frames</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="legend">🏆 = best (lowest frame mean). Blue left-edge = the
{vlabel} clip under test. Conclusion thresholds: &lt;0.30 Real, &lt;0.55
Neutral/Uncertain, ≥0.55 Fake. Generated by
<code>oldcam-testing/run_ab_test.py</code>; scores reuse the existing
sidecar JSONs (only the {vlabel} clip cost an API call).</p>
</div></body></html>"""
    out.write_text(doc, encoding="utf-8")
    _log(f"  -> HTML report: {out.relative_to(_REPO_ROOT)}")
    return out


def append_scoreboard(ranked, source: Path, version: str) -> None:
    if not SCOREBOARD.exists():
        SCOREBOARD.write_text(
            "# Oldcam A/B Scoreboard\n\nResemble scores — **lower = "
            "better**.\n", encoding="utf-8"
        )
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"\n## {ts} — {version} — `{source.name}`\n",
        "| Rank | Video | Frame mean | Frame min | Certainty | Verdict |",
        "|------|-------|-----------|-----------|-----------|---------|",
    ]
    for r in ranked:
        flag = " 🏆" if r.rank == 1 else ""
        lines.append(
            f"| {r.rank or '—'} | {r.name} | {_fmt(r.frame_mean)}{flag} "
            f"| {_fmt(r.frame_min)} | {_fmt(r.certainty)} "
            f"| {r.verdict_label or '—'} |"
        )
    with SCOREBOARD.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    _log(f"  -> scoreboard updated: {SCOREBOARD.relative_to(_REPO_ROOT)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Make oldcam vN, score only it, compare vs the corpus."
    )
    ap.add_argument("--version", default=DEFAULT_VERSION,
                    help="oldcam version to test (vN -> oldcam_vN.py). "
                         f"Default: {DEFAULT_VERSION}")
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE,
                    help="Source/Kling clip to run the version on.")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS,
                    help="Folder of already-scored clips + sidecars.")
    ap.add_argument("--no-score", action="store_true",
                    help="Produce the clip only; skip the API call.")
    ap.add_argument("--report-only", action="store_true",
                    help="Skip make+score; rebuild the report from "
                         "existing sidecars in --corpus.")
    args = ap.parse_args(argv)

    version = args.version.strip().lower()
    if not _VERSION_RE.match(version):
        _log(f"--version must look like 'v17' (got {args.version!r})")
        return 2

    corpus = args.corpus.resolve()
    source = args.source.resolve()
    if not corpus.is_dir():
        _log(f"Corpus folder not found: {corpus}")
        return 2

    if not args.report_only:
        if not source.is_file():
            _log(f"Source clip not found: {source}")
            return 2
        _log(f"[1/4] Making {version} from {source.name}")
        clip = make_clip(version, source, corpus)
        if clip is None:
            return 1
        if args.no_score:
            _log(f"\n--no-score: {version} produced. Score later, then "
                 "rerun with --report-only.")
            return 0
        _log(f"[2/4] Scoring {version} (single Resemble API call)")
        try:
            score_only(clip)
        except RuntimeError as e:  # missing key / API success=false
            _log(f"\nScoring failed: {e}")
            _log(f"{version} clip is still in the corpus; fix the key and "
                 "rerun with --report-only.")
            return 1

    _log("[3/4] Loading existing sidecars (no API) + ranking")
    ranked = collect_results(corpus)
    if not ranked:
        _log("No scored clips found in the corpus.")
        return 1

    _log("[4/4] Writing report + scoreboard")
    write_html(ranked, corpus, source, version)
    append_scoreboard(ranked, source, version)
    win = next((r for r in ranked if r.ok), None)
    _log(f"\nDone. Winner: {win.name if win else '—'} "
         f"(frame_mean {_fmt(win.frame_mean) if win else '—'}). "
         f"Open the HTML in oldcam-testing/reports/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
