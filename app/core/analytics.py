"""First-party, aggregate page-view counting.

What it stores: one Redis hash per UTC day, ``cp:pv:YYYY-MM-DD``, whose fields
are ``<page>|<source>`` and whose values are counts. That is all.

What it never stores: IP addresses, user agents, cookies, user ids, full
referrer URLs, or status-page slugs (those name an agency's own client). The
privacy policy promises this, and ``tests/test_analytics.py`` pins it.

Why not a third-party script: Cloudflare Web Analytics needs an API token
scope we do not hold and a client-side beacon; Google Analytics needs a consent
banner in the EU. Counting on the server costs one Redis HINCRBY per human page
view and answers the only questions that matter right now: is anyone visiting,
which page, and where did they come from.

Failure mode: counting must never break or slow a page. Every Redis call is
fire-and-forget with a short timeout, and any exception is swallowed.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from app.core.config import get_settings

KEY_PREFIX = "cp:pv:"
KEY_TTL_SECONDS = 400 * 24 * 3600

# Exact public pages worth counting. Anything else (dashboard, API, assets)
# is ignored, so a logged-in user clicking around does not inflate "traffic".
_EXACT = {
    "/": "/",
    "/docs/getting-started": "/docs/getting-started",
    "/legal/privacy": "/legal/privacy",
    "/legal/terms": "/legal/terms",
    "/dashboard/register": "/dashboard/register",
}
_VS_RE = re.compile(r"^/vs/([a-z0-9-]{1,40})/?$")
_STATUS_RE = re.compile(r"^/status/[^/]+/?$")

# Crawlers, link unfurlers, monitors and scripts. Matching any of these means
# "not a person", which is the only distinction the numbers need.
BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|preview|monitor|uptime|checkpulse|curl|wget|python|"
    r"httpx|aiohttp|go-http|java/|okhttp|headless|lighthouse|pingdom|facebookexternalhit|"
    r"embedly|whatsapp|discord|slack|telegram|bingpreview|scanner|axios|node-fetch|"
    r"libwww|postman|insomnia|feed|validator",
    re.I,
)

_TAG_RE = re.compile(r"[^a-z0-9._-]")
_SELF_HOSTS = {"checkpulse.dev", "www.checkpulse.dev", "trycheckpulse.com", "www.trycheckpulse.com"}

_client = None
_pending: set[asyncio.Task] = set()


def page_label(path: str) -> str | None:
    """Map a request path to the label we count, or None to ignore it."""
    if path in _EXACT:
        return _EXACT[path]
    m = _VS_RE.match(path)
    if m:
        return f"/vs/{m.group(1)}"
    if _STATUS_RE.match(path):
        # Collapse every status page into one bucket: the slug identifies an
        # agency's client and has no business sitting in our analytics.
        return "/status/*"
    return None


def _clean_tag(value: str) -> str:
    return _TAG_RE.sub("", value.strip().lower())[:40]


def traffic_source(query_string: str, referer: str | None) -> str:
    """Where a visit came from: explicit ?ref/utm_source tag, else referring host."""
    qs = parse_qs(query_string or "", keep_blank_values=False)
    for key in ("ref", "utm_source"):
        if qs.get(key):
            tag = _clean_tag(qs[key][0])
            if tag:
                return f"ref:{tag}"
    if referer:
        host = (urlsplit(referer).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host in _SELF_HOSTS or f"www.{host}" in _SELF_HOSTS:
            return "internal"
        if host:
            return _clean_tag(host) or "direct"
    return "direct"


def is_human(user_agent: str | None, accept: str | None) -> bool:
    if not user_agent or BOT_RE.search(user_agent):
        return False
    # Real browsers ask for HTML on a navigation; scripts usually do not.
    return bool(accept and "text/html" in accept)


def _redis():
    global _client
    if _client is None:
        from redis import asyncio as aioredis

        _client = aioredis.Redis.from_url(
            get_settings().redis_url,
            socket_timeout=0.3,
            socket_connect_timeout=0.3,
        )
    return _client


# ``?ref=`` is visitor-controlled, so someone could mint a new hash field per
# request. Past this many distinct fields in one day, new ones fold into
# "<page>|other" so the hash (and Redis memory on a full box) stays bounded.
MAX_FIELDS_PER_DAY = 500


async def _incr(day: str, field: str) -> None:
    try:
        r = _redis()
        key = KEY_PREFIX + day
        if await r.hlen(key) >= MAX_FIELDS_PER_DAY and not await r.hexists(key, field):
            field = field.split("|", 1)[0] + "|other"
        await r.hincrby(key, field, 1)
        await r.expire(key, KEY_TTL_SECONDS)
    except Exception:
        # Analytics is never allowed to surface as an error.
        pass


def record(page: str, source: str, now: datetime | None = None) -> None:
    day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    try:
        task = asyncio.get_running_loop().create_task(_incr(day, f"{page}|{source}"))
    except RuntimeError:
        return
    _pending.add(task)  # keep a strong reference until it finishes
    task.add_done_callback(_pending.discard)


class PageViewMiddleware:
    """Count successful human GETs of public pages. Pure ASGI, adds no latency."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return
        page = page_label(scope.get("path", ""))
        if page is None:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not is_human(headers.get("user-agent"), headers.get("accept")):
            await self.app(scope, receive, send)
            return

        status_code = {"value": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code["value"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        if status_code["value"] == 200:
            qs = scope.get("query_string", b"").decode("latin-1")
            record(page, traffic_source(qs, headers.get("referer")))
