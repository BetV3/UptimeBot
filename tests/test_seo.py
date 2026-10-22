"""Crawler and link-preview basics: sitemap, robots, OG tags on every public page."""
import re
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app.api.routes.seo import SITEMAP_PAGES
from app.main import app

client = TestClient(app)
BROWSER = {"user-agent": "Mozilla/5.0 Chrome/128", "accept": "text/html"}
PUBLIC = [p for p, _, _ in SITEMAP_PAGES]


def test_sitemap_is_valid_xml_listing_public_pages():
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(r.content)
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall("s:url/s:loc", ns)]
    assert "https://checkpulse.dev/" in locs
    assert all(l.startswith("https://checkpulse.dev/") for l in locs)
    assert not any("/status/" in l or "/dashboard" in l for l in locs)


@pytest.mark.parametrize("path", PUBLIC)
def test_every_sitemap_url_is_live(path):
    # A dead URL in the sitemap shows up as an error in Search Console.
    assert client.get(path, headers=BROWSER).status_code == 200


def test_robots_points_at_sitemap_and_hides_app():
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap: https://checkpulse.dev/sitemap.xml" in r.text
    for p in ("/dashboard/", "/api/", "/internal/"):
        assert f"Disallow: {p}" in r.text


def test_og_image_served():
    r = client.get("/static/og.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_indexnow_key_file():
    from app.api.routes.seo import INDEXNOW_KEY
    assert re.fullmatch(r"[a-f0-9]{32}", INDEXNOW_KEY)
    r = client.get(f"/{INDEXNOW_KEY}.txt")
    assert r.status_code == 200
    assert r.text == INDEXNOW_KEY


def _meta(html, attr, name):
    m = re.search(rf'<meta {attr}="{re.escape(name)}" content="([^"]*)"', html)
    return m.group(1) if m else None


@pytest.mark.parametrize("path", PUBLIC + ["/dashboard/register", "/dashboard/login"])
def test_public_pages_have_description_and_og_tags(path):
    html = client.get(path, headers=BROWSER).text
    desc = _meta(html, "name", "description")
    assert desc and 50 <= len(desc) <= 200, desc
    assert _meta(html, "property", "og:image") == "https://checkpulse.dev/static/og.png"
    assert _meta(html, "property", "og:title")
    assert _meta(html, "name", "twitter:card") == "summary_large_image"
    canon = re.search(r'<link rel="canonical" href="([^"]+)"', html).group(1)
    assert canon == "https://checkpulse.dev" + path


def test_dashboard_pages_are_noindex():
    html = client.get("/dashboard/login", headers=BROWSER).text
    assert '<meta name="robots" content="noindex">' in html
    html = client.get("/", headers=BROWSER).text
    assert 'name="robots" content="noindex"' not in html


def test_no_em_dashes_in_new_marketing_copy():
    # House style: no em dashes in copy we add (seo partial).
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/templates/_seo_head.html").read_text()
    assert "\u2014" not in src


def test_status_page_footer_link_is_attributed():
    from app.api.routes import status
    import inspect
    assert 'href="/?ref=status-page"' in inspect.getsource(status._render_status_page)
