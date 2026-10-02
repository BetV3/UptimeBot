"""Crawler-facing routes: robots.txt and sitemap.xml.

Cloudflare's managed robots.txt currently answers /robots.txt at the edge with
content-signal boilerplate and no Sitemap line. Serving our own here only
takes effect once that managed file is switched off in the dashboard
(Security -> Bots -> "Manage your robots.txt"); until then this route is
shadowed but harmless.

The sitemap lists public marketing pages only. Status pages are deliberately
excluded: their slugs name an agency's own clients, and an agency that makes a
page public for its client has not asked us to submit it to Google.
"""
from datetime import date

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

router = APIRouter()

BASE = "https://checkpulse.dev"

# (path, changefreq, priority). Add a page here when it ships; a test asserts
# every listed path returns 200 so a dead URL never reaches Search Console.
SITEMAP_PAGES: list[tuple[str, str, str]] = [
    ("/", "weekly", "1.0"),
    ("/tools/ssl-checker", "monthly", "0.8"),
    ("/vs/uptimerobot", "monthly", "0.8"),
    ("/docs/getting-started", "monthly", "0.7"),
    ("/legal/privacy", "yearly", "0.2"),
    ("/legal/terms", "yearly", "0.2"),
]

ROBOTS_TXT = f"""User-agent: *
Allow: /
Disallow: /dashboard/
Disallow: /api/
Disallow: /internal/
Disallow: /auth/
Disallow: /billing/

Sitemap: {BASE}/sitemap.xml
"""


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse(ROBOTS_TXT, headers={"Cache-Control": "public, max-age=3600"})


def build_sitemap(pages: list[tuple[str, str, str]], lastmod: str) -> str:
    urls = "".join(
        f"<url><loc>{BASE}{path}</loc><lastmod>{lastmod}</lastmod>"
        f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
        for path, freq, prio in pages
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    body = build_sitemap(SITEMAP_PAGES, date.today().isoformat())
    return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})
