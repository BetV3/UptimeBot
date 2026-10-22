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

from fastapi import APIRouter, BackgroundTasks, Request
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


# ── Multi-site check (/tools/site-check) ─────────────────────────────────────
# Real checks from the US workers, so the limits are tighter than the SSL tool:
# one run is up to 10 sites x 3 regions of worker time. Turnstile on the form,
# per-visitor and global caps, fail CLOSED. See app/services/site_check.py.

SITE_PER_MINUTE = 3
SITE_PER_HOUR = 10
# Global budget in SITES, not runs. Workers run checks one at a time and retry a
# timeout once, so a dead site costs a worker up to 2 x CHECK_TIMEOUT + 0.5 s.
# 60 sites/h x 12.5 s = 750 s/h, at most ~21% of each worker even if every
# site is a black hole. Normal use (a few runs of ~5 sites) is far below.
SITE_GLOBAL_SITES_PER_HOUR = 60
_ssl_sem = asyncio.Semaphore(4)


async def site_allowed(visitor: str, n_sites: int) -> str | None:
    try:
        if await _hit(f"cp:tool:site:m:{visitor}", 60) > SITE_PER_MINUTE:
            return "Too many checks. Wait a minute and try again."
        if await _hit(f"cp:tool:site:h:{visitor}", 3600) > SITE_PER_HOUR:
            return "Hourly limit reached. Try again later, or monitor these sites for free with an account."
        r = _redis()
        used = await r.incrby("cp:tool:site:global_sites", n_sites)
        if used == n_sites:
            await r.expire("cp:tool:site:global_sites", 3600)
        if used > SITE_GLOBAL_SITES_PER_HOUR:
            return "The checker is busy right now. Try again in a little while."
    except Exception:
        return "The checker is temporarily unavailable. Try again shortly."
    return None


def _site_ctx(request: Request, **kw) -> dict:
    from app.core.turnstile import turnstile_site_key
    from app.services import site_check
    ctx = {"request": request, "sites_input": "", "error": None, "run": None, "run_id": None,
           "max_sites": site_check.MAX_SITES, "turnstile_site_key": turnstile_site_key()}
    ctx.update(kw)
    return ctx


@router.get("/tools/site-check", response_class=HTMLResponse)
async def site_check_form(request: Request):
    return _templates().TemplateResponse("tool_site_check.html", _site_ctx(request))


async def _vet_sites(sites: list):
    """(ok sites, [(input, reason)]) using the same rules as a saved monitor."""
    from app.services import target_guard
    ok, bad = [], []
    for s in sites:
        if s.error:
            bad.append((s.raw[:60], s.error))
            continue
        try:
            await run_in_threadpool(target_guard.check_url, s.url)
        except target_guard.BlockedTarget as e:
            bad.append((s.name, str(e).rstrip(".")))
            continue
        ok.append(s)
    return ok, bad


@router.post("/tools/site-check", response_class=HTMLResponse)
async def site_check_start(request: Request, background: BackgroundTasks):
    from fastapi.responses import RedirectResponse
    from app.core.database import async_session
    from app.core.turnstile import CHALLENGE_FAILED_MESSAGE, verify_turnstile
    from app.services import bulk_add, site_check

    form = await request.form()
    raw = str(form.get("sites") or "")[:5000]

    def fail(msg: str, code: int = 400):
        return _templates().TemplateResponse(
            "tool_site_check.html", _site_ctx(request, sites_input=raw, error=msg), status_code=code)

    if not await verify_turnstile(str(form.get("cf-turnstile-response") or ""), request):
        return fail(CHALLENGE_FAILED_MESSAGE, 403)
    parsed = bulk_add.parse(raw)
    # Count before any DNS work, then spend the rate limit, then resolve: an
    # oversized or repeated paste never makes the server resolve anything.
    if sum(1 for s in parsed if not s.error) > site_check.MAX_SITES:
        return fail(f"Check up to {site_check.MAX_SITES} sites at a time "
                    f"({sum(1 for s in parsed if not s.error)} entered). A free account monitors more.")
    if not any(not s.error for s in parsed):
        bad = next((s for s in parsed if s.error), None)
        reason = f" ({bad.raw[:60]}: {bad.error})" if bad else ""
        return fail("Enter at least one public website, one per line." + reason)
    reason = await site_allowed(visitor_key(request), sum(1 for s in parsed if not s.error))
    if reason:
        return fail(reason, 429)
    sites, bad = await _vet_sites(parsed)
    if not sites:
        return fail("Enter at least one public website, one per line." + (f" ({bad[0][0]}: {bad[0][1]})" if bad else ""))

    async with async_session() as db:
        regions = await site_check.eligible_regions(db)
        if not regions:
            return fail("The checker is temporarily unavailable. Try again shortly.", 503)
        run_id = await site_check.start_run(db, _redis(), sites, regions)

    https_hosts = sorted({s.host for s in sites if s.url.startswith("https://")})

    async def read_certs():
        res = await site_check.gather_ssl(https_hosts, probe, _ssl_sem, wall=40)
        await site_check.attach_ssl(_redis(), run_id, res)

    background.add_task(read_certs)
    return RedirectResponse(f"/tools/site-check/r/{run_id}", status_code=303)


@router.get("/tools/site-check/r/{run_id}", response_class=HTMLResponse)
async def site_check_result(request: Request, run_id: str):
    import re as _re
    from app.core.database import async_session
    from app.services import site_check

    if not _re.fullmatch(r"[A-Za-z0-9_-]{8,32}", run_id):
        return _templates().TemplateResponse("tool_site_check.html", _site_ctx(
            request, error="That results link is not valid."), status_code=404)
    try:
        async with async_session() as db:
            run = await site_check.load(db, _redis(), run_id)
    except Exception:
        run = None
        return _templates().TemplateResponse("tool_site_check.html", _site_ctx(
            request, error="The checker is temporarily unavailable. Try again shortly."), status_code=503)
    if run is None:
        return _templates().TemplateResponse("tool_site_check.html", _site_ctx(
            request, error="These results have expired (links last 24 hours). Run the check again."),
            status_code=404)
    v = site_check.view(run)
    return _templates().TemplateResponse(
        "tool_site_check.html", _site_ctx(request, run=v, run_id=run_id, sites_input=v["urls"]),
        headers={"Cache-Control": "no-store"})
