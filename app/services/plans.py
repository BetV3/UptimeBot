from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Monitor, PlanType, Project, User


@dataclass(frozen=True)
class PlanLimits:
    max_projects: int | None  # None = unlimited
    max_monitors: int
    min_interval_seconds: int


PLAN_LIMITS: dict[PlanType, PlanLimits] = {
    PlanType.FREE: PlanLimits(max_projects=1, max_monitors=3, min_interval_seconds=300),
    PlanType.STARTER: PlanLimits(max_projects=10, max_monitors=40, min_interval_seconds=60),
    PlanType.PRO: PlanLimits(max_projects=50, max_monitors=200, min_interval_seconds=30),
}


# 14-day Pro trial, no card. Pro features, but tighter numbers than paid Pro:
# the checks run on three small worker VPSes, and a trial must not be a way to
# get 200 monitors at 30 s for free. Approved by Elvis 2026-10-22.
TRIAL_DAYS = 14
TRIAL_LIMITS = PlanLimits(max_projects=50, max_monitors=100, min_interval_seconds=60)


def trial_active(user: User, now: datetime | None = None) -> bool:
    """True while a no-card Pro trial is running. A paid plan always wins."""
    ends = getattr(user, "trial_ends_at", None)
    return user.plan == PlanType.FREE and ends is not None and ends > (now or datetime.now(timezone.utc))


def get_limits(user: User, now: datetime | None = None) -> PlanLimits:
    """Effective limits: the trial's while it runs on a Free account, else the plan's."""
    if trial_active(user, now):
        return TRIAL_LIMITS
    return PLAN_LIMITS[user.plan]


def limits_label(user: User) -> str:
    """How limit messages name the account's level: "free plan", "Pro trial"..."""
    return "Pro trial" if trial_active(user) else f"{user.plan.value} plan"


async def check_project_limit(user: User, db: AsyncSession) -> None:
    limits = get_limits(user)
    if limits.max_projects is None:
        return
    count = (
        await db.execute(
            select(func.count()).select_from(Project).where(Project.user_id == user.id)
        )
    ).scalar()
    if count >= limits.max_projects:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your {limits_label(user)} allows up to {limits.max_projects} project(s). Upgrade to create more.",
        )


async def check_monitor_limit(user: User, db: AsyncSession) -> None:
    limits = get_limits(user)
    # Count monitors across ALL of the user's projects
    count = (
        await db.execute(
            select(func.count())
            .select_from(Monitor)
            .join(Project, Monitor.project_id == Project.id)
            .where(Project.user_id == user.id)
        )
    ).scalar()
    if count >= limits.max_monitors:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your {limits_label(user)} allows up to {limits.max_monitors} monitors. Upgrade to create more.",
        )


def check_interval_limit(user: User, interval_seconds: int) -> None:
    limits = get_limits(user)
    if interval_seconds < limits.min_interval_seconds:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your {limits_label(user)} requires a minimum interval of {limits.min_interval_seconds} seconds. Upgrade for faster checks.",
        )


async def check_resume_allowed(user: User, monitor: Monitor, db: AsyncSession) -> None:
    """Refuse to resume a paused monitor past the account's limits.

    Monitors can outnumber the limits after a trial ends or a plan shrinks:
    the extras are paused, not deleted. Resuming one must not get around that,
    so it needs a free slot among RUNNING monitors and a project within the
    plan's project count (oldest projects first). Raises 403 otherwise.
    """
    limits = get_limits(user)
    if limits.max_projects is not None:
        allowed = (await db.execute(
            select(Project.id).where(Project.user_id == user.id)
            .order_by(Project.created_at.asc(), Project.id.asc()).limit(limits.max_projects)
        )).scalars().all()
        if monitor.project_id not in set(allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your {limits_label(user)} covers {limits.max_projects} project(s), and this monitor is in another one. Upgrade to resume it.",
            )
    running = (await db.execute(
        select(func.count()).select_from(Monitor).join(Project, Monitor.project_id == Project.id)
        .where(Project.user_id == user.id, Monitor.is_active.is_(True))
    )).scalar() or 0
    if running >= limits.max_monitors:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your {limits_label(user)} runs up to {limits.max_monitors} monitors at once. Pause another one or upgrade to resume this.",
        )
