import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import Check, CheckStatus, User, Project, Monitor, HttpMethod, MonitorStatus
from app.schemas.monitors import MonitorCreate, MonitorUpdate, MonitorResponse
from app.services.auth import get_current_user

router = APIRouter()


def _monitor_to_response(m: Monitor) -> MonitorResponse:
    return MonitorResponse(
        id=str(m.id),
        project_id=str(m.project_id),
        name=m.name,
        url=m.url,
        method=m.method.value,
        expected_status=m.expected_status,
        interval_seconds=m.interval_seconds,
        timeout_seconds=m.timeout_seconds,
        headers=m.headers,
        body=m.body,
        is_active=m.is_active,
        current_status=m.current_status.value,
        created_at=m.created_at.isoformat(),
    )


async def _get_user_project(
    project_id: uuid.UUID, user: User, db: AsyncSession
) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


async def _get_user_monitor(
    monitor_id: uuid.UUID, user: User, db: AsyncSession
) -> Monitor:
    result = await db.execute(
        select(Monitor)
        .join(Project, Monitor.project_id == Project.id)
        .where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitor not found")
    return monitor


# --- Project-scoped routes ---


@router.post(
    "/projects/{project_id}/monitors",
    response_model=MonitorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_monitor(
    project_id: uuid.UUID,
    body: MonitorCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user, db)
    monitor = Monitor(
        project_id=project_id,
        name=body.name,
        url=body.url,
        method=HttpMethod(body.method),
        expected_status=body.expected_status,
        interval_seconds=body.interval_seconds,
        timeout_seconds=body.timeout_seconds,
        headers=body.headers,
        body=body.body,
    )
    db.add(monitor)
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


@router.get("/projects/{project_id}/monitors", response_model=list[MonitorResponse])
async def list_monitors(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user, db)
    result = await db.execute(
        select(Monitor).where(Monitor.project_id == project_id).order_by(Monitor.created_at.desc())
    )
    return [_monitor_to_response(m) for m in result.scalars().all()]


# --- Monitor-scoped routes ---


@router.get("/monitors/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    return _monitor_to_response(monitor)


@router.patch("/monitors/{monitor_id}", response_model=MonitorResponse)
async def update_monitor(
    monitor_id: uuid.UUID,
    body: MonitorUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    update_data = body.model_dump(exclude_unset=True)
    if "method" in update_data:
        update_data["method"] = HttpMethod(update_data["method"])
    for field, value in update_data.items():
        setattr(monitor, field, value)
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


@router.delete("/monitors/{monitor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    await db.delete(monitor)
    await db.commit()


@router.post("/monitors/{monitor_id}/pause", response_model=MonitorResponse)
async def pause_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    monitor.is_active = False
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


@router.post("/monitors/{monitor_id}/resume", response_model=MonitorResponse)
async def resume_monitor(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    monitor = await _get_user_monitor(monitor_id, current_user, db)
    monitor.is_active = True
    await db.commit()
    await db.refresh(monitor)
    return _monitor_to_response(monitor)


# --- Check history ---


@router.get("/monitors/{monitor_id}/checks")
async def list_checks(
    monitor_id: uuid.UUID,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_monitor(monitor_id, current_user, db)

    offset = (page - 1) * per_page
    result = await db.execute(
        select(Check)
        .where(Check.monitor_id == monitor_id)
        .order_by(Check.checked_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    checks = result.scalars().all()

    total_result = await db.execute(
        select(func.count()).select_from(Check).where(Check.monitor_id == monitor_id)
    )
    total = total_result.scalar()

    return {
        "checks": [
            {
                "id": str(c.id),
                "region": c.region.value,
                "status": c.status.value,
                "status_code": c.status_code,
                "response_time_ms": c.response_time_ms,
                "error": c.error,
                "checked_at": c.checked_at.isoformat(),
            }
            for c in checks
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/monitors/{monitor_id}/checks/summary")
async def checks_summary(
    monitor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await _get_user_monitor(monitor_id, current_user, db)

    now = datetime.now(timezone.utc)
    periods = {
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
    }

    summary = {}
    for label, delta in periods.items():
        since = now - delta
        total_result = await db.execute(
            select(func.count())
            .select_from(Check)
            .where(Check.monitor_id == monitor_id, Check.checked_at >= since)
        )
        total = total_result.scalar()

        if total == 0:
            summary[label] = {"uptime_pct": None, "total_checks": 0}
            continue

        up_result = await db.execute(
            select(func.count())
            .select_from(Check)
            .where(
                Check.monitor_id == monitor_id,
                Check.checked_at >= since,
                Check.status == CheckStatus.UP,
            )
        )
        up_count = up_result.scalar()
        summary[label] = {
            "uptime_pct": round((up_count / total) * 100, 3),
            "total_checks": total,
        }

    return summary
