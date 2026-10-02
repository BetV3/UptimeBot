"""Free public tools. Today: the SSL certificate checker.

Abuse controls, because this makes our server open connections on behalf of
anonymous visitors (the signup form was abused for months before Turnstile):

- ssl_probe enforces port 443, public addresses only, and connects to the
  vetted IP (no rebinding). See app/services/ssl_probe.py.
- Redis fixed-window limits: per visitor 10/min and 60/hour, and a global
  ceiling of 600/hour so a distributed flood cannot turn the box into a
  scanner. Limits fail CLOSED: if Redis is unavailable the tool says so and
  does nothing.
- At most 4 lookups run at once; each is capped at 12s wall time.
- Visitor key is CF-Connecting-IP. That header is trustworthy here only
  because the origin binds 127.0.0.1 and is reachable solely through the
  Cloudflare tunnel, which overwrites any client-supplied value.
- Nothing about the lookup is stored or logged by us.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.services.ssl_probe import ProbeError, probe

router = APIRouter()

PER_MINUTE = 10
PER_HOUR = 60
GLOBAL_PER_HOUR = 600
WALL_TIMEOUT = 12.0
_sem = asyncio.Semaphore(4)
_redis_client = None


def _templates() -> Jinja2Templates:
    from app.main import templates  # shared instance (globals, filters)
    return templates


def _redis():
    global _redis_client
    if _redis_client is None:
        from redis import asyncio as aioredis

        _redis_client = aioredis.Redis.from_url(
            get_settings().redis_url, socket_timeout=0.5, socket_connect_timeout=0.5
        )
    return _redis_client


def visitor_key(request: Request) -> str:
    ip = request.headers.get("cf-connecting-ip", "").strip()
    if not ip and request.client:
        ip = request.client.host
    return ip or "unknown"


async def _hit(key: str, window: int) -> int:
    r = _redis()
    n = await r.incr(key)
    if n == 1:
        await r.expire(key, window)
    return n


async def allowed(visitor: str) -> str | None:
    """None if this lookup may run, else a user-facing reason. Fails closed."""
    try:
        if await _hit(f"cp:tool:ssl:m:{visitor}", 60) > PER_MINUTE:
            return "Too many lookups. Wait a minute and try again."
        if await _hit(f"cp:tool:ssl:h:{visitor}", 3600) > PER_HOUR:
            return "Hourly lookup limit reached. Try again later."
        if await _hit("cp:tool:ssl:global", 3600) > GLOBAL_PER_HOUR:
            return "The checker is busy right now. Try again in a little while."
    except Exception:
        return "The checker is temporarily unavailable. Try again shortly."
    return None


@router.get("/tools/ssl-checker", response_class=HTMLResponse)
async def ssl_checker(request: Request, host: str | None = None):
    ctx = {"request": request, "host_input": (host or "")[:300], "report": None, "error": None}
    status_code = 200
    if host is not None and host.strip():
        reason = await allowed(visitor_key(request))
        if reason:
            ctx["error"], status_code = reason, 429
        else:
            try:
                async with _sem:
                    ctx["report"] = await asyncio.wait_for(
                        run_in_threadpool(probe, host[:300]), timeout=WALL_TIMEOUT
                    )
            except ProbeError as e:
                ctx["error"] = str(e)
            except asyncio.TimeoutError:
                ctx["error"] = "The lookup took too long. The site may be slow or unreachable on port 443."
            except Exception:
                ctx["error"] = "Something went wrong checking that domain. Try again."
    return _templates().TemplateResponse(
        "tool_ssl_checker.html", ctx, status_code=status_code,
        headers={"Cache-Control": "no-store"} if host else {},
    )
