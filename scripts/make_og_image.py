#!/usr/bin/env python3
"""Render app/static/og.png (1200x630), the link-preview image for social/Slack/Discord unfurls.

Run: .venv/bin/python scripts/make_og_image.py
Uses only fonts present on the build host (Inter + Liberation Mono); the PNG is
committed, so production never needs Pillow or these fonts.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1200, 630
BG = (10, 14, 26)
INK = (248, 250, 252)
MUTED = (148, 163, 184)
DIM = (100, 116, 139)
BRAND = (124, 58, 237)
OK = (16, 185, 129)
BAD = (239, 68, 68)
CARD = (17, 24, 39)
LINE = (30, 41, 59)

OUT = Path(__file__).resolve().parents[1] / "app" / "static" / "og.png"
SANS_B = "/usr/share/fonts/opentype/inter/Inter-Bold.otf"
SANS_M = "/usr/share/fonts/opentype/inter/Inter-Medium.otf"
MONO = "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"
MONO_B = "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"


def f(path, size):
    return ImageFont.truetype(path, size)


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# dot grid, same motif as the site background
for x in range(16, W, 32):
    for y in range(16, H, 32):
        d.point((x, y), fill=(28, 34, 50))

# soft brand glow, top right
glow = Image.new("RGB", (W, H), BG)
gd = ImageDraw.Draw(glow)
gd.ellipse((760, -260, 1400, 380), fill=(58, 28, 120))
glow = glow.filter(ImageFilter.GaussianBlur(120))
img = Image.blend(img, glow, 0.55)
d = ImageDraw.Draw(img)

# wordmark
d.text((72, 64), "Check", font=f(MONO_B, 40), fill=INK)
d.text((72 + d.textlength("Check", font=f(MONO_B, 40)), 64), "Pulse", font=f(MONO_B, 40), fill=BRAND)

# headline (58px keeps the longest line clear of the card at x=780)
d.text((72, 150), "Uptime monitoring for", font=f(SANS_B, 58), fill=INK)
d.text((72, 222), "agencies that manage", font=f(SANS_B, 58), fill=INK)
d.text((72, 294), "client websites.", font=f(SANS_B, 58), fill=INK)

d.text((72, 396), "Down only when 2 of 3 regions agree.", font=f(SANS_M, 30), fill=MUTED)
d.text((72, 438), "Branded status page per client.", font=f(SANS_M, 30), fill=MUTED)

# region consensus card
cx, cy, cw, ch = 780, 150, 348, 300
d.rounded_rectangle((cx, cy, cx + cw, cy + ch), radius=14, fill=CARD, outline=LINE, width=2)
d.text((cx + 24, cy + 22), "// consensus check", font=f(MONO, 20), fill=DIM)
rows = [("US-EAST", "DOWN", BAD), ("EU-WEST", "DOWN", BAD), ("AP-SOUTH", "UP", OK)]
for i, (region, state, col) in enumerate(rows):
    y = cy + 70 + i * 56
    d.ellipse((cx + 24, y + 6, cx + 38, y + 20), fill=col)
    d.text((cx + 52, y), region, font=f(MONO, 22), fill=INK)
    d.text((cx + cw - 24 - d.textlength(state, font=f(MONO_B, 22)), y), state, font=f(MONO_B, 22), fill=col)
d.line((cx + 24, cy + 238, cx + cw - 24, cy + 238), fill=LINE, width=2)
d.text((cx + 24, cy + 252), "2/3 agree", font=f(MONO_B, 22), fill=BAD)
d.text((cx + cw - 24 - d.textlength("ALERT", font=f(MONO_B, 22)), cy + 252), "ALERT", font=f(MONO_B, 22), fill=BAD)

# footer url + accent bar
d.text((72, 548), "checkpulse.dev", font=f(MONO, 26), fill=DIM)
d.rectangle((0, H - 8, W, H), fill=BRAND)

OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT, "PNG", optimize=True)
print(OUT, OUT.stat().st_size, "bytes")
