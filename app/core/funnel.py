"""Aggregate signup-funnel counters.

Same rules as the page-view counter in ``app/core/analytics.py``:

What it stores: one Redis hash per UTC day, ``cp:funnel:YYYY-MM-DD``. Fields
are a fixed set of step names (``STEPS``), plus ``account_created|<source>``
for where new accounts came from (site-check, status-page, ssl-checker,
direct, ...). Values are counts. That is all.

What it never stores: email addresses, IP addresses, user agents, user ids,
project ids, run ids, cookies or full referrer URLs. ``tests/test_funnel.py``
pins this, the same way ``tests/test_analytics.py`` pins the page-view rule.

Why: page views say people reach /dashboard/register; they do not say where
signups die (the challenge, a typo in the password, the verification email,
an empty dashboard). These eight counters answer that without tracking anyone.

Failure mode: a counter is never allowed to break or noticeably slow a signup.
Every call is bounded by a short timeout and every exception is caught and
logged (without any request data), then ignored.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from app.core.config import get_settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "cp:funnel:"
KEY_TTL_SECONDS = 400 * 24 * 3600   # about 13 months, same as page views
TIMEOUT_SECONDS = 0.5

# The signup path, in order. record() refuses anything else, so the hash can
# never grow a field from user input.
STEPS: tuple[str, ...] = (
    "register_submit",        # POST /dashboard/register (or /auth/register) arrived
    "turnstile_failed",       # the human check rejected it
    "validation_failed",      # passwords differ, too short, email already registered
    "account_created",        # a users row was written
    "email_verified",         # the verification link was opened
    "first_project_created",  # the account's first project
    "first_monitor_created",  # the account's first monitor (form, bulk add or API)
    "checkout_started",       # a Stripe checkout session was created
)

# Where an account came from. ``?ref=`` is visitor-controlled, so only short
# lowercase tags are kept (no dots or @, so an email or a hostname can never
# land here), and past MAX_SOURCES_PER_DAY distinct tags new ones fold into
# "other".
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
MAX_SOURCES_PER_DAY = 50

# Same-origin pages that link to signup, mapped to a source tag, for visitors
# who arrive at /dashboard/register without an explicit ?ref=.
_PAGE_SOURCES = (
    (re.compile(r"^/tools/site-check(/|$)"), "site-check"),
    (re.compile(r"^/tools/ssl-checker/?$"), "ssl-checker"),
    (re.compile(r"^/status/[^/]+/?$"), "status-page"),
    (re.compile(r"^/vs/([a-z0-9-]{1,40})/?$"), "vs-{0}"),
)
_SELF_HOSTS = {"checkpulse.dev", "www.checkpulse.dev", "trycheckpulse.com", "www.trycheckpulse.com"}

_client = None


def _redis():
    """Lazily built async Redis client with short timeouts."""
    global _client
    if _client is None:
        from redis import asyncio as aioredis

        _client = aioredis.Redis.from_url(
            get_settings().redis_url,
            socket_timeout=0.3,
            socket_connect_timeout=0.3,
        )
    return _client


def clean_source(value: str | None) -> str:
    """Normalize a source tag; anything that is not a short plain tag becomes "other"."""
    if not value:
        return "direct"
    tag = value.strip().lower()[:40]
    return tag if SOURCE_RE.match(tag) else "other"


def signup_source(query_ref: str | None, referer: str | None, own_host: str | None = None) -> str:
    """Where a visitor to the signup form came from, as a short tag.

    Order: an explicit ``?ref=`` on the signup URL; else the ``?ref=`` of the
    same-site page they came from (the landing page linked from a status page
    footer carries ``?ref=status-page``); else which of our pages linked them;
    else "direct". Only the tag is returned, never the URL.
    """
    if query_ref:
        return clean_source(query_ref)
    if not referer:
        return "direct"
    try:
        parts = urlsplit(referer)
    except ValueError:
        return "direct"
    host = (parts.hostname or "").lower()
    own = _SELF_HOSTS | ({own_host.lower()} if own_host else set())
    if host not in own:
        return "external" if host else "direct"
    qs = parse_qs(parts.query or "")
    for key in ("ref", "utm_source"):
        if qs.get(key):
            return clean_source(qs[key][0])
    for pattern, tag in _PAGE_SOURCES:
        m = pattern.match(parts.path or "/")
        if m:
            return clean_source(tag.format(*m.groups()))
    return "direct"


async def _incr(day: str, step: str, source: str | None) -> None:
    """Add one to the step (and to its source field) in that day's hash."""
    r = _redis()
    key = KEY_PREFIX + day
    await r.hincrby(key, step, 1)
    if source is not None:
        field = f"{step}|{source}"
        if not await r.hexists(key, field):
            names = [f.decode() if isinstance(f, bytes) else f for f in await r.hkeys(key)]
            if sum(1 for f in names if "|" in f) >= MAX_SOURCES_PER_DAY:
                field = f"{step}|other"
        await r.hincrby(key, field, 1)
    await r.expire(key, KEY_TTL_SECONDS)


async def record(step: str, source: str | None = None, now: datetime | None = None) -> None:
    """Count one occurrence of a funnel step. Never raises, never waits long.

    ``source`` is only meaningful for ``account_created``; it is cleaned with
    clean_source() so a caller cannot store free text by mistake.
    """
    try:
        if step not in STEPS:
            logger.warning("funnel: unknown step ignored")
            return
        src = clean_source(source) if source is not None else None
        day = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        await asyncio.wait_for(_incr(day, step, src), timeout=TIMEOUT_SECONDS)
    except Exception as exc:  # counting must never break signup
        logger.warning("funnel: counter for %s failed (%s)", step, type(exc).__name__)
