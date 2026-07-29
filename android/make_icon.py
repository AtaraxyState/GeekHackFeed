#!/usr/bin/env python3
"""Draw the launcher icon at every density aapt2 expects.

Keeps a PNG mipmap set rather than a vector, because some launchers still
render adaptive vectors inconsistently.
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

BG = (23, 25, 34)
KEY_SIDE = (58, 64, 96)
KEY_TOP = (124, 140, 255)
LEGEND = (14, 15, 19)


def draw_icon(size: int) -> Image.Image:
    # Supersample, then downscale, so the small densities stay crisp.
    scale = 4
    px = size * scale
    img = Image.new("RGBA", (px, px), BG + (255,))
    draw = ImageDraw.Draw(img)

    unit = px / 100.0
    # keycap body
    body = [12 * unit, 20 * unit, 88 * unit, 84 * unit]
    draw.rounded_rectangle(body, radius=10 * unit, fill=KEY_SIDE)
    # top face, offset up to suggest the profile
    top = [19 * unit, 14 * unit, 81 * unit, 64 * unit]
    draw.rounded_rectangle(top, radius=8 * unit, fill=KEY_TOP)
    # legend
    bar_w, bar_h = 26 * unit, 7 * unit
    cx, cy = 50 * unit, 39 * unit
    draw.rounded_rectangle(
        [cx - bar_w / 2, cy - bar_h / 2, cx + bar_w / 2, cy + bar_h / 2],
        radius=bar_h / 2,
        fill=LEGEND,
    )

    return img.resize((size, size), Image.LANCZOS)


def main():
    for density, size in DENSITIES.items():
        out_dir = os.path.join(HERE, "res", f"mipmap-{density}")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "ic_launcher.png")
        draw_icon(size).save(path, "PNG", optimize=True)
        print(f"  {density:<8} {size:>3}px  {path}")


if __name__ == "__main__":
    main()
