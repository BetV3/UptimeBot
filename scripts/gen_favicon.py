#!/usr/bin/env python3
"""Generate CheckPulse favicon assets.

Design notes
------------
The mark is an ECG/heartbeat trace — the product monitors uptime, and a pulse
line reads as "alive / still beating" at a glance. It matches the wordmark
already in base.html (`Check` + brand-purple `Pulse`).

Constraints that drove the drawing:

- A favicon is rendered at 16x16 in a browser tab. Anything with interior
  detail turns to mush at that size, so this is a *solid* brand-purple tile
  with a single high-contrast white stroke — two tones, no gradients, no text.
- The tile is filled rather than transparent-on-dark so the icon stays legible
  in light-themed tabs, bookmark bars, and on the iOS home screen.
- Everything is drawn at 16x supersample and downscaled with LANCZOS, which
  antialiases the diagonal pulse strokes far better than drawing at target size.
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static"
OUT.mkdir(parents=True, exist_ok=True)

BRAND = (124, 58, 237, 255)      # #7C3AED — tailwind `brand` in base.html
INK = (248, 250, 252, 255)       # #F8FAFC — tailwind `ink`
SS = 16                          # supersample factor


def draw_icon(size: int, *, padding_ratio: float = 0.0, radius_ratio: float = 0.22) -> Image.Image:
    """Render the mark at `size` px by supersampling and downscaling.

    padding_ratio insets the tile (used for the maskable/apple icon, which
    gets cropped by the platform); radius_ratio is the corner rounding.

    Not used for 16px — see draw_icon_16().
    """
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = int(s * padding_ratio)
    box = (pad, pad, s - pad - 1, s - pad - 1)
    d.rounded_rectangle(box, radius=int((s - 2 * pad) * radius_ratio), fill=BRAND)

    # Heartbeat polyline in normalized tile coords: flat, small dip, tall
    # spike, overshoot, back to flat. The tall spike is deliberately
    # off-centre so the silhouette is asymmetric and recognisable when small.
    inner = s - 2 * pad
    pts_norm = [
        (0.12, 0.50),
        (0.30, 0.50),
        (0.38, 0.62),
        (0.48, 0.24),
        (0.58, 0.74),
        (0.66, 0.50),
        (0.88, 0.50),
    ]
    pts = [(pad + x * inner, pad + y * inner) for x, y in pts_norm]

    width = max(1, int(inner * 0.085))
    d.line(pts, fill=INK, width=width, joint="curve")
    # Round the endpoints and vertices; PIL's joint="curve" leaves butt caps.
    r = width / 2
    for x, y in pts:
        d.ellipse((x - r, y - r, x + r, y + r), fill=INK)

    return img.resize((size, size), Image.LANCZOS)


# A 16x16 favicon has ~196 usable interior pixels. Downscaling the vector mark
# into that grid antialiases every diagonal into half-tone grey, and the dip,
# spike and overshoot smear into a single blob that reads as an asterisk. At
# this size the icon has to be authored as pixels, not resampled into them.
#
# Legend: '.' transparent, 'B' brand purple, 'W' white.
#
# The trace is one continuous 1px line, 8-connected end to end: baseline enters
# left on row 10, rises through col 6 to the peak at col 7 (rows 5-8), falls
# through col 8 past the baseline into the overshoot (rows 11-12), then returns
# to row 10 and exits right. Both baselines sit on the SAME row so the shape
# reads as a pulse rather than as a step, and every column overlaps its
# neighbour by at least one row so there are no floating segments.
_ICON_16 = [
    "....BBBBBBBB....",
    "..BBBBBBBBBBBB..",
    ".BBBBBBBBBBBBBB.",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBBBBBBBBBB",
    "BBBBBBBWBBBBBBBB",
    "BBBBBBBWBBBBBBBB",
    "BBBBBBBWBBBBBBBB",
    "BBBBBBWWBBBBBBBB",
    "BBBBBBWBWBBBBBBB",
    "BWWWWWWBWBWWWWWB",
    "BBBBBBBBWWWBBBBB",
    "BBBBBBBBWWBBBBBB",
    "BBBBBBBBBBBBBBBB",
    ".BBBBBBBBBBBBBB.",
    "..BBBBBBBBBBBB..",
]


def draw_icon_16() -> Image.Image:
    """Pixel-exact 16x16 favicon. See _ICON_16 for why this is hand-authored."""
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = img.load()
    for y, row in enumerate(_ICON_16):
        for x, ch in enumerate(row):
            if ch == "B":
                px[x, y] = BRAND
            elif ch == "W":
                px[x, y] = INK
    return img


def main() -> None:
    written = []

    # Multi-resolution .ico for legacy browsers and Windows pinned sites.
    # The 16px frame is the hand-authored one; larger frames are resampled.
    ico_sizes = [16, 32, 48, 64, 128, 256]
    ico_path = OUT / "favicon.ico"
    draw_icon(256).save(
        ico_path, format="ICO", sizes=[(n, n) for n in ico_sizes]
    )
    written.append(ico_path)

    # PNGs referenced explicitly by <link> tags.
    p16 = OUT / "favicon-16x16.png"
    draw_icon_16().save(p16, format="PNG", optimize=True)
    written.append(p16)

    for name, size, kwargs in [
        ("favicon-32x32.png", 32, {}),
        ("icon-192.png", 192, {}),
        ("icon-512.png", 512, {}),
        # iOS crops to a squircle and adds its own shadow; inset the art and
        # square the tile so the platform mask doesn't clip the pulse line.
        ("apple-touch-icon.png", 180, {"padding_ratio": 0.06, "radius_ratio": 0.0}),
    ]:
        p = OUT / name
        draw_icon(size, **kwargs).save(p, format="PNG", optimize=True)
        written.append(p)

    for p in written:
        print(f"{p.relative_to(OUT.parent.parent)}  {p.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
