"""Pin the favicon/static-asset wiring.

A favicon regression is silent — no error, no failing request, just a blank
tab icon nobody notices for weeks. These tests assert the assets exist, are
mounted, and are actually referenced by the pages users land on.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

client = TestClient(app)


@pytest.mark.parametrize(
    "name",
    [
        "favicon.ico",
        "favicon-16x16.png",
        "favicon-32x32.png",
        "apple-touch-icon.png",
        "icon-192.png",
        "icon-512.png",
        "site.webmanifest",
    ],
)
def test_asset_file_exists(name):
    """Generated assets are committed, not built at deploy time — the
    Dockerfile does a plain `COPY . .` with no asset pipeline."""
    p = STATIC / name
    assert p.is_file(), f"{name} missing from app/static/"
    assert p.stat().st_size > 0


def test_root_favicon_ico_is_served():
    """Browsers hit /favicon.ico directly, independent of any <link> tag.
    Without the explicit route this 404s on every page load."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    # ICO magic number: 00 00 01 00
    assert r.content[:4] == b"\x00\x00\x01\x00"


def test_favicon_is_cacheable():
    """An uncached favicon is re-fetched on every navigation."""
    r = client.get("/favicon.ico")
    assert "max-age" in r.headers.get("cache-control", "")


@pytest.mark.parametrize(
    "path,content_type",
    [
        ("/static/favicon-16x16.png", "image/png"),
        ("/static/favicon-32x32.png", "image/png"),
        ("/static/apple-touch-icon.png", "image/png"),
        ("/static/icon-192.png", "image/png"),
        ("/static/site.webmanifest", None),
    ],
)
def test_static_mount_serves_assets(path, content_type):
    r = client.get(path)
    assert r.status_code == 200, f"{path} not served"
    if content_type:
        assert r.headers["content-type"] == content_type


def test_manifest_is_valid_json_and_icons_resolve():
    r = client.get("/static/site.webmanifest")
    data = r.json()
    assert data["name"]
    assert data["icons"], "manifest declares no icons"
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, (
            f"manifest references {icon['src']} which does not resolve"
        )


@pytest.mark.parametrize("page", ["/", "/dashboard/login", "/dashboard/register"])
def test_pages_reference_the_favicon(page):
    """Covers both the base-template pages and the standalone landing page,
    which does not extend base.html and so needs its own head tags."""
    r = client.get(page)
    assert r.status_code == 200
    assert 'rel="icon"' in r.text, f"{page} has no favicon link tag"
    assert "/favicon.ico" in r.text


def test_16px_icon_is_hand_authored_not_resampled():
    """The 16px variant must stay pixel-exact: two tones only, no antialiased
    intermediate greys. Resampling the vector mark into a 16px grid smears the
    trace into an unreadable blob, which is why draw_icon_16() exists."""
    from PIL import Image

    img = Image.open(STATIC / "favicon-16x16.png").convert("RGBA")
    opaque = {px[:3] for px in img.getdata() if px[3] > 0}
    assert opaque == {(124, 58, 237), (248, 250, 252)}, (
        f"expected exactly brand + white, got {sorted(opaque)}"
    )


def test_16px_trace_is_one_connected_line():
    """Every white pixel must belong to a single 8-connected component.
    Disconnected fragments read as a barcode, not a heartbeat."""
    from scripts.gen_favicon import _ICON_16

    white = {
        (x, y)
        for y, row in enumerate(_ICON_16)
        for x, ch in enumerate(row)
        if ch == "W"
    }
    assert white

    seen: set = set()
    stack = [next(iter(white))]
    while stack:
        x, y = stack.pop()
        if (x, y) in seen:
            continue
        seen.add((x, y))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nxt = (x + dx, y + dy)
                if nxt in white and nxt not in seen:
                    stack.append(nxt)

    assert seen == white, (
        f"{len(white) - len(seen)} white pixels are disconnected from the trace"
    )


def test_16px_baselines_are_level():
    """The entry and exit baselines must sit on the same row. When they don't,
    the mark reads as a step/staircase rather than a pulse."""
    from scripts.gen_favicon import _ICON_16

    left = [y for y, row in enumerate(_ICON_16) if row[1] == "W"]
    right = [y for y, row in enumerate(_ICON_16) if row[14] == "W"]
    assert left and right, "trace does not span the full width"
    assert left == right, f"baseline enters at row {left} but exits at row {right}"
