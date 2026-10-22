from dataclasses import dataclass

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


def get_limits(user: User) -> PlanLimits:
    return PLAN_LIMITS[user.plan]


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
            detail=f"Your {user.plan.value} plan allows up to {limits.max_projects} project(s). Upgrade to create more.",
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
            detail=f"Your {user.plan.value} plan allows up to {limits.max_monitors} monitors. Upgrade to create more.",
        )


def check_interval_limit(user: User, interval_seconds: int) -> None:
    limits = get_limits(user)
    if interval_seconds < limits.min_interval_seconds:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Your {user.plan.value} plan requires a minimum interval of {limits.min_interval_seconds} seconds. Upgrade for faster checks.",
        )
