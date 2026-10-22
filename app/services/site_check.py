"""Free multi-site check (/tools/site-check): up to 10 sites, checked by the real US workers.

An agency pastes its client sites and sees, per site and per US location, whether it
answers, how fast, whether a location gets blocked (401/403/429: hosts that refuse
data-center networks), and the SSL certificate's days left. Results live at a private
link for 24 hours so they can be shared with a client.

How a run works, and why it is shaped this way:

- Each site becomes a throwaway, INACTIVE monitor in one system project, plus one
  pending_check per eligible region. The scheduler ignores inactive monitors, so it
  never re-checks them. Workers pick the pending_checks up through the normal lease
  path, and /internal/jobs applies the same dispatch-time SSRF check as for customers.
- Eligible regions = scheduled, heartbeat under 2 minutes old, worker version 1.1.0 or
  newer (the release that vets every address at connect time and on each redirect).
  Older workers never see a public visitor's URL.
- The SSL certificate is read once from the central server with ssl_probe (port 443,
  public addresses only), while the workers run.
- When every site has an answer from every region, or WAIT_SECONDS pass, the results
  are copied to Redis and the throwaway monitors are deleted. Runs nobody looked at
  are swept after SWEEP_AFTER. /internal/results drops results for deleted monitors,
  so a late worker post can't fail a whole batch.
- Limits (route): per visitor and global, fail closed; Turnstile on the form.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.models import (
    Check, HttpMethod, Monitor, MonitorType, PendingCheck, PlanType, Project, User, WorkerHeartbeat,
)
from app.services import regions as region_svc

MAX_SITES = 10
CHECK_TIMEOUT = 6              # seconds per check on the worker (see the budget in routes/tools.py)
WAIT_SECONDS = 75              # stop waiting for a region after this
RUN_TTL = 24 * 3600            # results link lifetime
SWEEP_AFTER = timedelta(minutes=15)
HEARTBEAT_FRESH = timedelta(minutes=2)
MIN_WORKER_VERSION = (1, 1, 0)
BLOCKED_CODES = {401, 403, 429}
SLOW_MS = 3000

SYSTEM_EMAIL = "site-check@system.checkpulse.invalid"
SYSTEM_SLUG = "system-site-check"
_KEY = "cp:sitecheck:run:"


def _version(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in (v or "").strip().split(".")[:3])
    except ValueError:
        return ()


async def eligible_regions(db) -> list[region_svc.Region]:
    """Scheduled regions whose worker is alive and runs the guarded release."""
    sched = {r.code: r for r in region_svc.scheduled(await region_svc.all_regions(db))}
    cutoff = datetime.now(timezone.utc) - HEARTBEAT_FRESH
    rows = (await db.execute(select(WorkerHeartbeat.region, WorkerHeartbeat.version, WorkerHeartbeat.last_seen)
                             .where(WorkerHeartbeat.region.in_(list(sched))))).all()
    ok = [code for code, ver, seen in rows
          if seen is not None and seen >= cutoff and _version(ver) >= MIN_WORKER_VERSION]
    return [sched[c] for c in sorted(ok)]


async def _system_project_id(db) -> uuid.UUID:
    """The one project that owns throwaway monitors. Created on first use, race-safe."""
    pid = (await db.execute(select(Project.id).where(Project.slug == SYSTEM_SLUG))).scalar_one_or_none()
    if pid:
        return pid
    from app.services.auth import hash_password
    await db.execute(pg_insert(User).values(
        id=uuid.uuid4(), email=SYSTEM_EMAIL, plan=PlanType.PRO, email_verified=False,
        # a real hash of a discarded random secret: the account can never log in
        password_hash=hash_password(secrets.token_urlsafe(32)),
    ).on_conflict_do_nothing(index_elements=["email"]))
    uid = (await db.execute(select(User.id).where(User.email == SYSTEM_EMAIL))).scalar_one()
    await db.execute(pg_insert(Project).values(
        id=uuid.uuid4(), user_id=uid, name="Public site check (system)", slug=SYSTEM_SLUG,
    ).on_conflict_do_nothing(index_elements=["slug"]))
    await db.commit()
    return (await db.execute(select(Project.id).where(Project.slug == SYSTEM_SLUG))).scalar_one()


async def sweep(db) -> int:
    """Delete throwaway monitors from runs nobody came back for."""
    pid = (await db.execute(select(Project.id).where(Project.slug == SYSTEM_SLUG))).scalar_one_or_none()
    if not pid:
        return 0
    res = await db.execute(delete(Monitor).where(
        Monitor.project_id == pid, Monitor.created_at < func.now() - SWEEP_AFTER))
    await db.commit()
    return res.rowcount or 0


async def start_run(db, redis, sites: list, regions: list[region_svc.Region]) -> str:
    """sites: bulk_add.Site objects that passed validation. Returns the run id."""
    await sweep(db)
    pid = await _system_project_id(db)
    run_id = secrets.token_urlsafe(12)
    now = datetime.now(timezone.utc)
    entries = []
    for i, s in enumerate(sites):
        m = Monitor(project_id=pid, name=f"sc-{run_id}-{i}", type=MonitorType.HTTP, url=s.url,
                    method=HttpMethod.GET, expected_status=200, interval_seconds=3600,
                    timeout_seconds=CHECK_TIMEOUT, is_active=False, next_check_at=None)
        db.add(m)
        await db.flush()
        for r in regions:
            db.add(PendingCheck(id=uuid.uuid4(), monitor_id=m.id, region=r.code, scheduled_at=now))
        entries.append({"name": s.name, "url": s.url, "host": s.host, "monitor_id": str(m.id)})
    await db.commit()
    run = {"id": run_id, "created": time.time(), "done": False, "sites": entries,
           "regions": [{"code": r.code, "label": r.display} for r in regions], "ssl": {}}
    await redis.set(_KEY + run_id, json.dumps(run), ex=RUN_TTL)
    return run_id


async def attach_ssl(redis, run_id: str, ssl: dict) -> None:
    # Separate key: the run record is rewritten when the run finalizes, and a
    # read-modify-write here could race with that and drop the certificates.
    await redis.set(_KEY + run_id + ":ssl", json.dumps(ssl), ex=RUN_TTL)


def ssl_summary(report) -> dict:
    return {"days_left": report.days_left, "verdict": report.verdict, "issuer": report.issuer,
            "trust_error": report.trust_error,
            "expires": report.not_after.strftime("%b %d, %Y") if report.not_after else None}


def classify(check: dict | None) -> str:
    if check is None:
        return "pending"
    code = check.get("status_code")
    if check.get("status") == "up":
        return "slow" if (check.get("ms") or 0) >= SLOW_MS else "up"
    if code in BLOCKED_CODES:
        return "blocked"
    if code is not None and code >= 500:
        return "error"
    if code is not None:
        return "unexpected"
    return "down"


def site_verdict(cells: list[str]) -> str:
    """One line per site, worst-first, from its per-region cells."""
    real = [c for c in cells if c != "pending"]
    if not real:
        return "no answer"
    if all(c in ("up", "slow") for c in real):
        return "slow" if "slow" in real else "up"
    if "blocked" in real and any(c in ("up", "slow") for c in real):
        return "blocked from some locations"
    if all(c == "blocked" for c in real):
        return "blocked"
    if any(c in ("up", "slow") for c in real):
        return "down from some locations"
    return "down"


async def load(db, redis, run_id: str) -> dict | None:
    """Current state of a run; finalizes it (copy to Redis, delete monitors) when complete."""
    raw = await redis.get(_KEY + run_id)
    if not raw:
        return None
    run = json.loads(raw)
    ssl_raw = await redis.get(_KEY + run_id + ":ssl")
    run["ssl"] = json.loads(ssl_raw) if ssl_raw else {}
    if run["done"]:
        return run
    mids = [uuid.UUID(s["monitor_id"]) for s in run["sites"]]
    rows = (await db.execute(select(Check).where(Check.monitor_id.in_(mids))
                             .order_by(Check.checked_at.asc()))).scalars().all()
    await db.commit()
    latest: dict[tuple[str, str], dict] = {}
    for c in rows:
        latest[(str(c.monitor_id), c.region)] = {
            "status": c.status.value, "status_code": c.status_code, "ms": c.response_time_ms,
            "error": (c.error or "")[:200] or None}
    codes = [r["code"] for r in run["regions"]]
    for s in run["sites"]:
        s["results"] = {code: latest.get((s["monitor_id"], code)) for code in codes}
    complete = all(v is not None for s in run["sites"] for v in s["results"].values())
    if complete or time.time() - run["created"] > WAIT_SECONDS:
        run["done"] = True
        stored = {k: v for k, v in run.items() if k != "ssl"}
        await redis.set(_KEY + run_id, json.dumps(stored), keepttl=True)
        await db.execute(delete(Monitor).where(Monitor.id.in_(mids)))
        await db.commit()
    return run


def view(run: dict) -> dict:
    """Template-ready rows."""
    codes = [r["code"] for r in run["regions"]]
    rows = []
    for s in run["sites"]:
        res = s.get("results") or {}
        cells = []
        for code in codes:
            c = res.get(code)
            kind = classify(c) if (c is not None or not run["done"]) else "no answer"
            cells.append({"code": code, "kind": kind, "check": c})
        rows.append({"name": s["name"], "url": s["url"], "host": s["host"], "cells": cells,
                     "verdict": site_verdict([x["kind"] for x in cells]) if run["done"] or all(
                         x["kind"] != "pending" for x in cells) else "checking",
                     "ssl": (run.get("ssl") or {}).get(s["host"])})
    return {"rows": rows, "regions": run["regions"], "done": run["done"],
            "urls": "\n".join(s["url"] for s in run["sites"])}


async def gather_ssl(hosts: list[str], probe, sem: asyncio.Semaphore, wall: float) -> dict:
    """{host: summary | {"error": str}} for each https host, bounded by sem and wall time."""
    from starlette.concurrency import run_in_threadpool
    from app.services.ssl_probe import ProbeError

    async def one(h):
        async with sem:
            try:
                return h, ssl_summary(await run_in_threadpool(probe, h))
            except ProbeError as e:
                return h, {"error": str(e)}
            except Exception:
                return h, {"error": "Could not read the certificate."}

    try:
        done = await asyncio.wait_for(asyncio.gather(*(one(h) for h in hosts)), timeout=wall)
    except asyncio.TimeoutError:
        return {}
    return dict(done)
