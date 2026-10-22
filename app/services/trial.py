"""14-day Pro trial with no card (approved by Elvis 2026-10-22).

Rules:

- Any account with a verified email can start ONE trial (``users.trial_used``).
- While it runs on a Free account, ``plans.get_limits()`` returns
  ``plans.TRIAL_LIMITS`` (50 projects, 100 monitors, 60 s) and Pro features.
  The numbers are below paid Pro on purpose: the checks run on three small
  worker VPSes.
- A paid Stripe plan always wins: ``get_limits()`` ignores the trial on a paid
  plan, and the billing webhook clears it (``clear_for_paid_plan``), so a later
  cancellation drops to Free instead of resurrecting the trial.
- On expiry (hourly ``trial_housekeeping`` task) the account goes back to Free
  limits. Monitors over the Free limit are PAUSED, never deleted: the oldest
  project keeps its oldest active monitors (3 on Free) running, with the
  interval raised to the Free minimum; everything else is paused with
  ``paused_reason = "trial_ended"``.
- Upgrading to a paid plan resumes monitors paused by the plan, oldest first,
  up to the new plan's limits (``fit_to_limits``). A monitor the user paused
  themselves (``paused_reason`` NULL) is never touched.
- One transactional reminder email about 3 days before the end, through
  ``app/services/mailer.py`` (the transactional lane).

The planning step is a pure function (``plan_fit``) so the "exactly which
monitors" rule is unit-tested without a database. Everything that writes uses
a synchronous Session, so the Celery task calls it directly and async routes
call it through ``AsyncSession.run_sync``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.models import Monitor, PlanType, Project, User
from app.services.plans import PLAN_LIMITS, TRIAL_DAYS, PlanLimits, get_limits, trial_active

logger = logging.getLogger(__name__)

PAUSED_TRIAL_ENDED = "trial_ended"   # paused when the trial ran out
PAUSED_PLAN_LIMIT = "plan_limit"     # paused when a smaller paid plan replaced the trial
PLAN_PAUSE_REASONS = (PAUSED_TRIAL_ENDED, PAUSED_PLAN_LIMIT)
REMINDER_BEFORE = timedelta(days=3)
PUBLIC_BASE_URL = "https://checkpulse.dev"   # links in the reminder when app_url is a dev default
RESUME_STAGGER_SECONDS = 2           # same spread as bulk add, so resumed checks do not fire at once


class TrialError(Exception):
    """A trial cannot be started; the message is safe to show the user."""


# ── state ────────────────────────────────────────────────────────────────────

def eligible(user: User) -> bool:
    """True if this account may start its one trial right now."""
    return bool(user.email_verified) and not user.trial_used and user.plan == PlanType.FREE


def days_left(user: User, now: datetime | None = None) -> int:
    """Whole days left in a running trial, rounded up (0 when none is running)."""
    if not trial_active(user, now):
        return 0
    remaining = user.trial_ends_at - (now or datetime.now(timezone.utc))
    return max(1, -(-int(remaining.total_seconds()) // 86400))


def start(user: User, now: datetime | None = None) -> datetime:
    """Start the trial on a loaded user (caller commits). Returns the end time."""
    if user.plan != PlanType.FREE:
        raise TrialError("You already have a paid plan, which includes everything in the trial.")
    if not user.email_verified:
        raise TrialError("Verify your email address first, then start the trial.")
    if user.trial_used:
        raise TrialError("This account has already used its Pro trial.")
    now = now or datetime.now(timezone.utc)
    user.trial_used = True
    user.trial_offer = False
    user.trial_ends_at = now + timedelta(days=TRIAL_DAYS)
    user.trial_reminder_sent_at = None
    user.trial_expired_at = None
    return user.trial_ends_at


# ── which monitors run under a given set of limits ──────────────────────────

@dataclass(frozen=True)
class MonitorRow:
    """What plan_fit needs to know about one monitor."""
    id: object
    project_id: object
    is_active: bool
    paused_reason: str | None


def plan_fit(project_ids_oldest_first: list, monitors_oldest_first: list[MonitorRow],
             limits: PlanLimits) -> tuple[list, list]:
    """Decide which monitors may run under ``limits``: (run ids, pause ids).

    Only monitors that are running, or were paused by the plan, are candidates;
    a monitor the user paused stays out of both lists. Projects beyond
    ``max_projects`` (oldest kept) get none. Within the allowed projects the
    oldest candidates win, project by project, up to ``max_monitors``.
    """
    allowed = project_ids_oldest_first if limits.max_projects is None \
        else project_ids_oldest_first[:limits.max_projects]
    rank = {pid: i for i, pid in enumerate(allowed)}
    candidates = [m for m in monitors_oldest_first
                  if m.is_active or m.paused_reason in PLAN_PAUSE_REASONS]
    in_allowed = sorted((m for m in candidates if m.project_id in rank),
                        key=lambda m: rank[m.project_id])   # stable: keeps monitor age order
    run = [m.id for m in in_allowed[:limits.max_monitors]]
    run_set = set(run)
    pause = [m.id for m in candidates if m.id not in run_set]
    return run, pause


def _load(session: Session, user_id) -> tuple[list, list[Monitor]]:
    """The user's project ids and monitors, both oldest first."""
    pids = list(session.execute(
        select(Project.id).where(Project.user_id == user_id)
        .order_by(Project.created_at.asc(), Project.id.asc())).scalars())
    monitors = list(session.execute(
        select(Monitor).join(Project, Monitor.project_id == Project.id)
        .where(Project.user_id == user_id)
        .order_by(Monitor.created_at.asc(), Monitor.id.asc())).scalars())
    return pids, monitors


def fit_to_limits(session: Session, user: User, reason: str, now: datetime | None = None) -> dict:
    """Pause or resume the user's monitors to match their current limits (caller commits).

    Running monitors over the limit are paused with ``reason``; plan-paused
    monitors that fit again are resumed, oldest first. Monitors that keep
    running get their interval raised to the plan minimum. Nothing is deleted.
    """
    now = now or datetime.now(timezone.utc)
    limits = get_limits(user, now)
    pids, monitors = _load(session, user.id)
    rows = [MonitorRow(m.id, m.project_id, m.is_active, m.paused_reason) for m in monitors]
    run, pause = plan_fit(pids, rows, limits)
    by_id = {m.id: m for m in monitors}
    resumed = paused = raised = 0
    for i, mid in enumerate(run):
        m = by_id[mid]
        if m.interval_seconds < limits.min_interval_seconds:
            m.interval_seconds = limits.min_interval_seconds
            raised += 1
        if not m.is_active:
            m.is_active = True
            m.paused_reason = None
            m.next_check_at = now + timedelta(seconds=min(resumed * RESUME_STAGGER_SECONDS, 120))
            resumed += 1
    for mid in pause:
        m = by_id[mid]
        if m.is_active:
            m.is_active = False
            m.paused_reason = reason
            paused += 1
    return {"resumed": resumed, "paused": paused, "interval_raised": raised}


def would_pause_on_expiry(session: Session, user: User) -> int:
    """How many running monitors the end of the trial would pause (for the reminder)."""
    pids, monitors = _load(session, user.id)
    rows = [MonitorRow(m.id, m.project_id, m.is_active, m.paused_reason) for m in monitors]
    _, pause = plan_fit(pids, rows, PLAN_LIMITS[PlanType.FREE])
    running = {m.id for m in monitors if m.is_active}
    return sum(1 for mid in pause if mid in running)


def paused_by_plan(session: Session, user_id) -> int:
    """Monitors currently paused by the plan (not by the user)."""
    return session.execute(
        select(func.count()).select_from(Monitor).join(Project, Monitor.project_id == Project.id)
        .where(Project.user_id == user_id, Monitor.is_active.is_(False),
               Monitor.paused_reason.in_(PLAN_PAUSE_REASONS))).scalar() or 0


# ── billing ─────────────────────────────────────────────────────────────────

def clear_for_paid_plan(session: Session, user: User) -> dict | None:
    """A paid subscription replaced the plan: end any trial, then fit monitors to it.

    Only acts when there is something to do (a trial on the account, or
    monitors paused by a plan), so ordinary subscription updates are untouched.
    Returns what changed, or None.
    """
    if user.plan == PlanType.FREE:
        return None
    had_trial = user.trial_ends_at is not None
    if had_trial:
        user.trial_ends_at = None
        user.trial_offer = False
    if not had_trial and not paused_by_plan(session, user.id):
        return None
    return fit_to_limits(session, user, PAUSED_PLAN_LIMIT)


# ── hourly housekeeping (Celery beat) ───────────────────────────────────────

def expire_due(session: Session, now: datetime | None = None) -> int:
    """Move every Free account whose trial ended back to Free limits. Idempotent."""
    now = now or datetime.now(timezone.utc)
    users = list(session.execute(
        select(User).where(User.plan == PlanType.FREE, User.trial_ends_at.is_not(None),
                           User.trial_ends_at <= now, User.trial_expired_at.is_(None))
        .with_for_update(skip_locked=True)).scalars())
    for user in users:
        result = fit_to_limits(session, user, PAUSED_TRIAL_ENDED, now)
        user.trial_expired_at = now
        session.commit()
        logger.info("trial ended for one account: %s", result)
    return len(users)


def reminder_text(user: User, will_pause: int, base_url: str) -> tuple[str, str, str]:
    """(subject, text, html) for the single "trial ends soon" email."""
    free = PLAN_LIMITS[PlanType.FREE]
    end = f"{user.trial_ends_at:%B} {user.trial_ends_at.day}"
    billing = f"{base_url.rstrip('/')}/dashboard/billing"
    if will_pause:
        what = (f"{will_pause} of your monitors will be paused, not deleted. Your first project keeps "
                f"its {free.max_monitors} oldest monitors running, checked every "
                f"{free.min_interval_seconds // 60} minutes.")
    else:
        what = (f"Everything you monitor fits the Free plan, so it keeps running, checked every "
                f"{free.min_interval_seconds // 60} minutes.")
    subject = "Your CheckPulse Pro trial ends in 3 days"
    text = (
        f"Your CheckPulse Pro trial ends on {end}.\n\n"
        f"After that your account goes back to the Free plan ({free.max_projects} project, "
        f"{free.max_monitors} monitors). {what}\n\n"
        f"To keep everything running, pick a plan here:\n{billing}\n\n"
        "Upgrading resumes paused monitors automatically, up to the new plan's limits.\n"
        "You are getting this because you started a Pro trial. It is the only reminder we send.\n"
    )
    html = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, system-ui, sans-serif; background:#0b0f17; color:#e6edf3; padding:32px;">
    <div style="max-width:480px; margin:0 auto; background:#111826; border:1px solid #1f2a3a; border-radius:12px; padding:32px;">
      <h1 style="margin:0 0 16px; font-size:20px;">Your Pro trial ends on {end}</h1>
      <p style="margin:0 0 16px; color:#9aa7b8;">After that your account goes back to the Free plan
        ({free.max_projects} project, {free.max_monitors} monitors). {what}</p>
      <p style="margin:0 0 24px;">
        <a href="{billing}" style="display:inline-block; background:#7c3aed; color:#fff; text-decoration:none;
           padding:12px 20px; border-radius:8px; font-weight:600;">Choose a plan</a>
      </p>
      <p style="margin:0; font-size:12px; color:#6b7a8f;">Upgrading resumes paused monitors automatically,
        up to the new plan's limits. This is the only reminder we send about your trial.</p>
    </div>
  </body>
</html>
"""
    return subject, text, html


def send_reminders(session: Session, send, base_url: str, now: datetime | None = None) -> int:
    """Email every running trial that ends within 3 days and has not been reminded.

    ``send(to, subject, text, html)`` is the transactional mailer. The row is
    marked only after a successful send, so a provider error retries next hour.
    """
    now = now or datetime.now(timezone.utc)
    users = list(session.execute(
        select(User).where(User.plan == PlanType.FREE, User.email_verified.is_(True),
                           User.trial_ends_at > now, User.trial_ends_at <= now + REMINDER_BEFORE,
                           User.trial_reminder_sent_at.is_(None))
        .with_for_update(skip_locked=True)).scalars())
    sent = 0
    for user in users:
        subject, text, html = reminder_text(user, would_pause_on_expiry(session, user), base_url)
        try:
            send(user.email, subject, text, html)
        except Exception as exc:
            logger.warning("trial reminder failed, will retry (%s)", type(exc).__name__)
            session.rollback()
            continue
        user.trial_reminder_sent_at = now
        session.commit()
        sent += 1
    return sent


# ── what the dashboard shows ────────────────────────────────────────────────

def banner(session: Session, user: User, now: datetime | None = None) -> dict | None:
    """The dashboard banner for this account, or None.

    During the trial: {"kind": "active", "days": N}. After it, while monitors
    are still paused by it: {"kind": "ended", "paused": N}.
    """
    now = now or datetime.now(timezone.utc)
    if trial_active(user, now):
        return {"kind": "active", "days": days_left(user, now)}
    if user.trial_used and user.plan == PlanType.FREE and user.trial_ends_at is not None:
        n = paused_by_plan(session, user.id)
        if n:
            return {"kind": "ended", "paused": n}
    return None
