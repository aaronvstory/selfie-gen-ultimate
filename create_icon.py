"""
Kling UI Icon Generator
Generates a professional multi-size .ico file for the app.

Design:
- Dark rounded-square background matching the app's dark theme (#2D2D30)
- Stylized camera lens with aperture blades in accent blue (#6496FF)
- Inner AI sparkle/crosshair element
- Clean, modern aesthetic that reads well at all sizes
"""

import sys
import math
from pathlib import Path


def draw_rounded_rect(draw, box, radius, fill, outline=None, outline_width=1):
    """Draw a filled rounded rectangle."""
    x0, y0, x1, y1 = box
    r = max(2, int(radius))

    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)
    draw.ellipse([x0, y0, x0 + 2*r, y0 + 2*r], fill=fill)
    draw.ellipse([x1 - 2*r, y0, x1, y0 + 2*r], fill=fill)
    draw.ellipse([x0, y1 - 2*r, x0 + 2*r, y1], fill=fill)
    draw.ellipse([x1 - 2*r, y1 - 2*r, x1, y1], fill=fill)

    if outline:
        draw.arc([x0, y0, x0 + 2*r, y0 + 2*r], 180, 270, fill=outline, width=outline_width)
        draw.arc([x1 - 2*r, y0, x1, y0 + 2*r], 270, 360, fill=outline, width=outline_width)
        draw.arc([x0, y1 - 2*r, x0 + 2*r, y1], 90, 180, fill=outline, width=outline_width)
        draw.arc([x1 - 2*r, y1 - 2*r, x1, y1], 0, 90, fill=outline, width=outline_width)
        draw.line([x0 + r, y0, x1 - r, y0], fill=outline, width=outline_width)
        draw.line([x0 + r, y1, x1 - r, y1], fill=outline, width=outline_width)
        draw.line([x0, y0 + r, x0, y1 - r], fill=outline, width=outline_width)
        draw.line([x1, y0 + r, x1, y1 - r], fill=outline, width=outline_width)


def render_icon(size):
    """Render a single icon at the given size."""
    from PIL import Image, ImageDraw

    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # --- Colors from the app's theme ---
    bg_color = (45, 45, 48, 255)           # #2D2D30  bg_main
    border_color = (90, 90, 94, 255)       # #5A5A5E  border
    accent = (100, 150, 255)               # #6496FF  accent_blue
    accent_bright = (140, 185, 255)        # Lighter variant
    accent_dim = (60, 100, 200)            # Darker variant
    white = (240, 245, 255, 255)
    white_soft = (220, 230, 255, 140)

    # --- Background rounded square ---
    pad = max(0, size // 20)
    radius = max(3, size // 5)
    draw_rounded_rect(
        draw,
        [pad, pad, size - 1 - pad, size - 1 - pad],
        radius,
        bg_color,
        border_color,
        max(1, size // 64),
    )

    cx = size * 0.50
    cy = size * 0.48  # Slightly above center for optical balance

    # --- Outer lens ring (gradient effect via concentric circles) ---
    outer_r = size * 0.36
    ring_width = max(2, size // 10)

    # Draw outer glow
    if size >= 32:
        glow_img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        glow_px = glow_img.load()
        glow_r = outer_r + ring_width * 0.8
        for py in range(size):
            for px in range(size):
                dx = px - cx
                dy = py - cy
                dist = math.sqrt(dx * dx + dy * dy)
                if dist < glow_r:
                    ring_dist = abs(dist - outer_r)
                    t = 1.0 - min(1.0, ring_dist / (ring_width * 1.5))
                    alpha = int(t * t * 40)
                    if alpha > 0:
                        glow_px[px, py] = (accent[0], accent[1], accent[2], alpha)
        img = Image.alpha_composite(img, glow_img)
        draw = ImageDraw.Draw(img)

    # Outer ring — bright accent
    lw = max(2, int(ring_width * 0.55))
    draw.ellipse(
        [cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r],
        outline=(*accent, 240),
        width=lw,
    )

    # Inner ring — slightly smaller, dimmer
    inner_ring_r = outer_r - ring_width * 0.7
    lw_inner = max(1, int(ring_width * 0.3))
    draw.ellipse(
        [cx - inner_ring_r, cy - inner_ring_r,
         cx + inner_ring_r, cy + inner_ring_r],
        outline=(*accent_dim, 160),
        width=lw_inner,
    )

    # --- Aperture blades (6 curved segments between the rings) ---
    if size >= 32:
        blade_r = (outer_r + inner_ring_r) / 2
        blade_count = 6
        blade_width = max(1, int(ring_width * 0.25))
        for i in range(blade_count):
            angle_start = i * (360 / blade_count) + 15
            angle_end = angle_start + 360 / blade_count - 30
            arc_r = blade_r
            draw.arc(
                [cx - arc_r, cy - arc_r, cx + arc_r, cy + arc_r],
                angle_start,
                angle_end,
                fill=(*accent_bright, 120),
                width=blade_width,
            )

    # --- Center dot (lens center / sensor) ---
    center_dot_r = max(1, size // 16)
    draw.ellipse(
        [cx - center_dot_r, cy - center_dot_r,
         cx + center_dot_r, cy + center_dot_r],
        fill=white,
    )

    # --- AI Sparkle / crosshair (4 small rays from center) ---
    if size >= 32:
        ray_len = size * 0.08
        ray_width = max(1, size // 40)
        for angle in [0, 90, 180, 270]:
            rad = math.radians(angle)
            start_r = center_dot_r + max(1, size // 32)
            end_r = start_r + ray_len
            x1 = cx + math.cos(rad) * start_r
            y1 = cy + math.sin(rad) * start_r
            x2 = cx + math.cos(rad) * end_r
            y2 = cy + math.sin(rad) * end_r
            draw.line([(x1, y1), (x2, y2)], fill=white_soft, width=ray_width)

    # --- Diagonal sparkle rays (45-degree, shorter) ---
    if size >= 48:
        ray_len_diag = size * 0.05
        ray_width_d = max(1, size // 48)
        for angle in [45, 135, 225, 315]:
            rad = math.radians(angle)
            start_r = center_dot_r + max(1, size // 24)
            end_r = start_r + ray_len_diag
            x1 = cx + math.cos(rad) * start_r
            y1 = cy + math.sin(rad) * start_r
            x2 = cx + math.cos(rad) * end_r
            y2 = cy + math.sin(rad) * end_r
            draw.line([(x1, y1), (x2, y2)], fill=(*accent_bright, 100), width=ray_width_d)

    # --- Top-left specular highlight on outer ring ---
    if size >= 48:
        spec_angle = math.radians(225)  # top-left
        spec_cx = cx + math.cos(spec_angle) * outer_r * 0.85
        spec_cy = cy + math.sin(spec_angle) * outer_r * 0.85
        spec_r = max(1, size // 18)
        draw.ellipse(
            [spec_cx - spec_r, spec_cy - spec_r,
             spec_cx + spec_r, spec_cy + spec_r],
            fill=(255, 255, 255, 60),
        )

    # --- "AI" text badge at bottom-right (large sizes only) ---
    if size >= 64:
        try:
            from PIL import ImageFont
            font_size = max(7, size // 10)
            try:
                font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", font_size)
            except Exception:
                try:
                    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", font_size)
                except Exception:
                    font = ImageFont.load_default()

            # Position at bottom-right corner
            text = "AI"
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]

            tx = size - pad - tw - size // 14
            ty = size - pad - th - size // 12

            # Badge background pill
            badge_pad = max(2, size // 32)
            draw_rounded_rect(
                draw,
                [tx - badge_pad, ty - badge_pad // 2,
                 tx + tw + badge_pad, ty + th + badge_pad // 2],
                max(2, badge_pad),
                (*accent_dim, 200),
            )

            # Text
            draw.text((tx, ty), text, font=font, fill=white)
        except Exception:
            pass

    return img


def create_ico(output_path: str = "kling_ui.ico"):
    """Create the multi-size .ico file."""
    try:
        from PIL import Image
    except ImportError:
        print("ERROR: Pillow is not installed. Run: pip install Pillow")
        sys.exit(1)

    print("Generating Kling UI icon...")
    sizes = [16, 32, 48, 64, 128, 256]
    images = []

    for size in sizes:
        print(f"  Rendering {size}x{size}...")
        img = render_icon(size)
        images.append(img)

    print(f"  Saving {output_path}...")
    base = images[-1]   # 256x256 is the base
    rest = images[:-1]  # [16, 32, 48, 64, 128]
    base.save(
        output_path,
        format='ICO',
        append_images=rest,
    )
    print(f"  Done! Icon saved to: {output_path}")
    size_bytes = Path(output_path).stat().st_size
    print(f"  File size: {size_bytes:,} bytes")

    # Also save kling_ui.png at 256x256 — used by macOS/Linux iconphoto
    # at GUI startup (see kling_gui/main_window.py::_set_app_icon). Tk on
    # Aqua silently ignores .ico, so PNG is the only viable iconphoto
    # source for the dock + window-list icon on macOS.
    # Use Path.with_suffix / with_name to survive case-variant inputs
    # (e.g. ``foo.ICO``) and avoid str.replace stomping unintended files
    # (CodeRabbit minor on 253a9b4).
    output = Path(output_path)
    png_path = str(output.with_suffix(".png"))
    images[-1].save(png_path, format='PNG', optimize=True)
    print(f"  PNG saved to: {png_path}")

    # Also save a separate preview path (legacy; some downstream tools
    # look for *_preview.png so we keep emitting it).
    preview_path = str(output.with_name(f"{output.stem}_preview.png"))
    images[-1].save(preview_path, format='PNG')
    print(f"  Preview saved to: {preview_path}")

    return output_path


if __name__ == "__main__":
    script_dir = Path(__file__).parent
    ico_path = str(script_dir / "kling_ui.ico")
    create_ico(ico_path)
    print("\nIcon generation complete!")
