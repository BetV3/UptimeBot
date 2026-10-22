"""Carry a public site-check run into a new (or existing) account.

The flow: a visitor checks up to 10 sites at /tools/site-check, presses
"Monitor these N sites free", signs up, verifies, signs in, and their first
project screen offers the bulk-add form already filled with those sites. They
still press the button, and plan limits still apply (bulk add reports what it
skipped and why).

How the run id travels, and who can read the list:

- The button links to /dashboard/register?ref=site-check&run=<run id>. The run
  id is the unguessable id of the results link (secrets.token_urlsafe(12)).
- Opening that link stores the run id in an httponly cookie (path /dashboard)
  that expires with the run, then drops it from the URL.
- At registration the run id is also bound to the new account in Redis
  (``cp:sitecheck:claim:<user id>``), with the run's remaining TTL. That makes
  it survive the verification link being opened in another browser.
- A signed-in user who opens the link (or still carries the cookie) gets the
  run bound to their own account the same way.

So the site list is only ever shown to someone who presented the run id, and
only to the account it was bound to. Nothing is copied: the list is read from
the run itself (``cp:sitecheck:run:<id>``, 24 h), so when the run expires the
offer disappears with it, and the claim key expires at the same moment.
"""
from __future__ import annotations

import json
import logging
import re

from app.core.config import get_settings

logger = logging.getLogger(__name__)

RUN_KEY = "cp:sitecheck:run:"      # written by app/services/site_check.py
CLAIM_KEY = "cp:sitecheck:claim:"  # + user id -> run id
COOKIE = "cp_sc_run"
COOKIE_PATH = "/dashboard"
SOURCE = "site-check"              # bulk-add form marker for an import submit
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")

_client = None


def _redis():
    """Async Redis client with short timeouts; a dashboard page must never hang on it."""
    global _client
    if _client is None:
        from redis import asyncio as aioredis

        _client = aioredis.Redis.from_url(
            get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
        )
    return _client


def valid_run_id(run_id: str | None) -> bool:
    """True when the value has the shape of a site-check run id."""
    return bool(run_id) and bool(RUN_ID_RE.fullmatch(run_id))


async def run_sites(run_id: str) -> tuple[list[str], int] | None:
    """(site URLs, seconds left) for a live run, or None if it is unknown or expired."""
    if not valid_run_id(run_id):
        return None
    r = _redis()
    raw = await r.get(RUN_KEY + run_id)
    if not raw:
        return None
    ttl = await r.ttl(RUN_KEY + run_id)
    if ttl is None or ttl <= 0:
        return None
    try:
        urls = [s["url"] for s in json.loads(raw)["sites"] if s.get("url")]
    except (ValueError, KeyError, TypeError):
        return None
    return (urls, int(ttl)) if urls else None


async def bind(user_id, run_id: str) -> int:
    """Attach a live run to an account. Returns the site count, 0 if the run is gone."""
    try:
        found = await run_sites(run_id)
        if not found:
            return 0
        urls, ttl = found
        await _redis().set(CLAIM_KEY + str(user_id), run_id, ex=ttl)
        return len(urls)
    except Exception as exc:
        logger.warning("site import: bind failed (%s)", type(exc).__name__)
        return 0


async def pending(user, cookie_run: str | None = None) -> list[str] | None:
    """Site URLs waiting to be added for this user, or None.

    A run id still sitting in the browser cookie is bound to the signed-in user
    first (the same-browser path: register, verify, sign in). A claim whose run
    has expired is deleted. Redis trouble means "nothing pending", never an error.
    """
    try:
        if valid_run_id(cookie_run):
            await bind(user.id, cookie_run)
        r = _redis()
        run_id = await r.get(CLAIM_KEY + str(user.id))
        if not run_id:
            return None
        run_id = run_id.decode() if isinstance(run_id, bytes) else run_id
        found = await run_sites(run_id)
        if not found:
            await r.delete(CLAIM_KEY + str(user.id))
            return None
        return found[0]
    except Exception as exc:
        logger.warning("site import: lookup failed (%s)", type(exc).__name__)
        return None


async def clear(user_id) -> None:
    """Forget the pending import for an account (added, or dismissed)."""
    try:
        await _redis().delete(CLAIM_KEY + str(user_id))
    except Exception as exc:
        logger.warning("site import: clear failed (%s)", type(exc).__name__)


def set_cookie(response, run_id: str, ttl: int, secure: bool) -> None:
    """Remember the run id in this browser until the run expires."""
    response.set_cookie(COOKIE, run_id, max_age=max(min(ttl, 24 * 3600), 1), path=COOKIE_PATH,
                        httponly=True, samesite="lax", secure=secure)


def drop_cookie(response, secure: bool) -> None:
    """Remove the run-id cookie."""
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="lax", secure=secure)
