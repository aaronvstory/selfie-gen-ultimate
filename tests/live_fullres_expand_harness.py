"""LIVE full-res expand harness — calls the REAL provider (fal/BFL).

NOT a pytest test (costs money). Run manually overnight to validate the
full-res 3:4 expand on real ID images at various sizes / providers / modes.

Usage:
    venv/Scripts/python.exe tests/live_fullres_expand_harness.py \
        --out F:/tmp/fullres_out --budget-usd 1.0 [--provider fal|bfl|both]

For each (image, mode, provider) it:
  - runs OutpaintGenerator.outpaint(..., full_res_plan=plan)
  - asserts the output canvas == plan.full_canvas and center crop is
    byte-identical to the original (pixel-perfect)
  - measures seam sharpness just outside the original border (jaggedness proxy)
  - writes a side-by-side + a zoom crop of the original region into --out
  - appends a row to REPORT.md
Every run logs its estimated cost so the budget cap is respected.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from outpaint_geometry import compute_full_res_expand_plan, compute_provider_caps  # noqa: E402
from outpaint_generator import OutpaintGenerator  # noqa: E402

# Cost estimate per expand (rough, conservative). fal ~$0.035/MP of OUTPUT the
# provider actually renders (a small ≤2MP canvas), BFL ~$0.05/image.
FAL_COST = 0.05
BFL_COST = 0.05


def _load_keys():
    cfg = {}
    p = "kling_config.json"
    if os.path.exists(p):
        # A malformed config is a real setup error — surface it, don't silently
        # fall through to "no keys" (which masquerades as an auth failure later).
        with open(p, encoding="utf-8") as fh:
            cfg = json.load(fh)
    fal = cfg.get("falai_api_key") or os.environ.get("FAL_KEY", "")
    bfl = cfg.get("bfl_api_key") or os.environ.get("BFL_API_KEY", "")
    free = cfg.get("freeimage_api_key") or os.environ.get("FREEIMAGE_API_KEY", "")
    return fal, bfl, free


def _seam_jaggedness(img: Image.Image, left, top, ow, oh, band=6):
    """Proxy for a visible seam: mean abs luminance gradient across the ring
    just OUTSIDE the original border. Lower = smoother. Compares the border row
    of the original to the first generated row beyond it."""
    a = np.asarray(img.convert("L"), dtype=np.int32)
    H, W = a.shape
    diffs = []
    # top seam
    if top >= 1:
        inside = a[top, left:left + ow]
        outside = a[top - 1, left:left + ow]
        diffs.append(np.abs(inside - outside).mean())
    if top + oh < H:
        inside = a[top + oh - 1, left:left + ow]
        outside = a[top + oh, left:left + ow]
        diffs.append(np.abs(inside - outside).mean())
    if left >= 1:
        inside = a[top:top + oh, left]
        outside = a[top:top + oh, left - 1]
        diffs.append(np.abs(inside - outside).mean())
    if left + ow < W:
        inside = a[top:top + oh, left + ow - 1]
        outside = a[top:top + oh, left + ow]
        diffs.append(np.abs(inside - outside).mean())
    return float(np.mean(diffs)) if diffs else 0.0


def run_case(gen, img_path, mode, aspect, pct, provider, use_bfl, out_dir, log,
             border_strategy="ai"):
    caps = compute_provider_caps("bfl" if use_bfl else "fal")
    with Image.open(img_path) as im:
        from PIL import ImageOps
        ow, oh = ImageOps.exif_transpose(im).size
    plan = compute_full_res_expand_plan(ow, oh, pct, caps, aspect)
    tag = f"{Path(img_path).parent.name}_{ow}x{oh}_{mode}_{border_strategy}_{provider}_p{pct}"
    out_path = str(Path(out_dir) / f"{tag}.png")
    log(f"\n=== {tag} ===")
    log(f"  plan: full {plan['full_canvas_w']}x{plan['full_canvas_h']} "
        f"scale={plan['scale_pct']}% provider-canvas {plan['canvas_w']}x{plan['canvas_h']}")
    # Crafted prompt: tell the model to extend ONLY the background at the edges
    # and explicitly not to reproduce the central document. (Experiment: does a
    # stronger edge-only instruction stop the duplicate-ID artifact?)
    border_prompt = os.environ.get("BORDER_PROMPT") or (
        "Extend the image outward by continuing ONLY the background surface "
        "visible at the edges of the frame. Keep the same table/surface, "
        "lighting and blur as the edges. Do NOT add, repeat, or draw any card, "
        "document, ID, license, text, letters, numbers or photo — the center "
        "already exists and must not be duplicated. Soft, out-of-focus, empty "
        "continuation of the edge background only."
    )
    res = gen.outpaint(
        image_path=img_path,
        output_folder=out_dir,
        output_path=out_path,
        composite_mode="preserve_seamless",
        provider=provider,
        full_res_plan=plan,
        prompt=border_prompt,
        poll_timeout_seconds=180,
        border_strategy=border_strategy,
    )
    if not res:
        log(f"  FAILED: {gen.get_last_outpaint_error_detail()}")
        return {"tag": tag, "ok": False, "error": gen.get_last_outpaint_error_detail()}
    out = Image.open(res).convert("RGB")
    fl, ft = plan["full_left"], plan["full_top"]
    center = np.asarray(out.crop((fl, ft, fl + ow, ft + oh)))
    with Image.open(img_path) as im:
        from PIL import ImageOps
        orig = np.asarray(ImageOps.exif_transpose(im).convert("RGB"))
    pixel_perfect = center.shape == orig.shape and np.array_equal(center, orig)
    canvas_ok = out.size == (plan["full_canvas_w"], plan["full_canvas_h"])
    jag = _seam_jaggedness(out, fl, ft, ow, oh)
    log(f"  canvas_ok={canvas_ok} pixel_perfect_center={pixel_perfect} seam_jag={jag:.1f}")
    # zoom crop of a corner of the original inside the result to eyeball fidelity
    zc = out.crop((fl, ft, fl + min(ow, 500), ft + min(oh, 500)))
    zc.save(str(Path(out_dir) / f"{tag}_origzoom.png"))
    # A case only PASSES if the provider returned a path AND both invariants
    # (canvas dims + pixel-perfect center) hold — otherwise it's a real failure.
    ok = bool(canvas_ok and pixel_perfect)
    return {"tag": tag, "ok": ok, "canvas_ok": canvas_ok,
            "pixel_perfect_center": pixel_perfect, "seam_jag": round(jag, 1),
            "out": res, "full_canvas": f"{plan['full_canvas_w']}x{plan['full_canvas_h']}",
            "error": "" if ok else "canvas/center invariant failed"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget-usd", type=float, default=1.0)
    ap.add_argument("--provider", choices=["fal", "bfl", "both"], default="fal")
    ap.add_argument("--images", nargs="*", help="explicit image paths")
    ap.add_argument("--images-file", help="text file, one image path per line "
                    "(avoids shell-splitting paths with spaces)")
    ap.add_argument("--strategies", nargs="*",
                    choices=["edge_extend", "ai", "bria"],
                    help="border strategies to test (default edge_extend+ai)")
    ap.add_argument("--pct", type=int, default=30)
    args = ap.parse_args()

    fal, bfl, free = _load_keys()
    os.makedirs(args.out, exist_ok=True)
    report = Path(args.out) / "REPORT.md"
    lines = []

    def log(m):
        print(m, flush=True)
        lines.append(m)

    if args.images_file:
        images = [ln.strip() for ln in open(args.images_file, encoding="utf-8")
                  if ln.strip()]
    else:
        images = args.images or ["test-material/canned-pipeline/front.jpg"]
    providers = (["fal", "bfl"] if args.provider == "both" else [args.provider])
    modes = [("three_four_fullres", (3, 4))]
    # edge_extend is free (no provider) -> always run it; ai costs.
    strategies = args.strategies or ["edge_extend", "ai"]

    spent = 0.0
    results = []
    gen = OutpaintGenerator(fal, freeimage_key=free, bfl_api_key=bfl)
    gen.set_progress_callback(lambda m, l: None)
    for strat in strategies:
        provs = providers if strat == "ai" else ["fal"]  # edge_extend ignores provider
        for prov in provs:
            use_bfl = prov == "bfl"
            if strat == "ai" and use_bfl and not bfl:
                log("skip bfl (no key)"); continue
            cost = 0.0 if strat == "edge_extend" else (BFL_COST if use_bfl else FAL_COST)
            for img in images:
                if not os.path.isfile(img):
                    log(f"skip missing {img}"); continue
                for mode, aspect in modes:
                    if spent + cost > args.budget_usd:
                        log(f"BUDGET REACHED (${spent:.2f}) — skipping {strat}/{prov}")
                        continue
                    r = run_case(gen, img, mode, aspect, args.pct, prov, use_bfl,
                                 args.out, log, border_strategy=strat)
                    spent += cost
                    results.append(r)

    log(f"\n\n## Summary  (est spend ${spent:.2f})")
    log("| case | ok | canvas | pixel-perfect | seam_jag |")
    log("|------|----|--------|---------------|----------|")
    for r in results:
        if r.get("ok"):
            log(f"| {r['tag']} | ✅ | {r.get('full_canvas')} | "
                f"{'✅' if r.get('pixel_perfect_center') else '❌'} | {r.get('seam_jag')} |")
        else:
            log(f"| {r['tag']} | ❌ {r.get('error','')} | | | |")
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport: {report}")


if __name__ == "__main__":
    main()
