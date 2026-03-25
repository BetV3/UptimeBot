import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import (
    Check, CheckStatus, Incident, Monitor, MonitorStatus,
    Project, StatusPage, User,
)
from app.schemas.status_page import StatusPageUpdate, StatusPageResponse
from app.services.auth import get_current_user

router = APIRouter()


# --- Authenticated settings endpoints ---


@router.get("/projects/{project_id}/status-page", response_model=StatusPageResponse)
async def get_status_page_settings(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(StatusPage)
        .join(Project, StatusPage.project_id == Project.id)
        .where(StatusPage.project_id == project_id, Project.user_id == current_user.id)
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Status page not found")
    return _sp_to_response(page)


@router.patch("/projects/{project_id}/status-page", response_model=StatusPageResponse)
async def update_status_page_settings(
    project_id: uuid.UUID,
    body: StatusPageUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(StatusPage)
        .join(Project, StatusPage.project_id == Project.id)
        .where(StatusPage.project_id == project_id, Project.user_id == current_user.id)
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Status page not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(page, field, value)
    await db.commit()
    await db.refresh(page)
    return _sp_to_response(page)


def _sp_to_response(sp: StatusPage) -> StatusPageResponse:
    return StatusPageResponse(
        id=str(sp.id),
        project_id=str(sp.project_id),
        is_public=sp.is_public,
        display_name=sp.display_name,
        logo_url=sp.logo_url,
        primary_color=sp.primary_color,
    )


# --- Public status page ---


@router.get("/status/{slug}", response_class=HTMLResponse)
async def public_status_page(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Look up project by slug
    result = await db.execute(
        select(Project, StatusPage)
        .join(StatusPage, StatusPage.project_id == Project.id)
        .where(Project.slug == slug, StatusPage.is_public.is_(True))
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Status page not found")

    project, status_page = row

    # Get monitors
    monitors_result = await db.execute(
        select(Monitor)
        .where(Monitor.project_id == project.id, Monitor.is_active.is_(True))
        .order_by(Monitor.name)
    )
    monitors = monitors_result.scalars().all()

    # Calculate 90-day uptime per monitor
    now = datetime.now(timezone.utc)
    since_90d = now - timedelta(days=90)
    monitor_data = []
    overall_up = True

    for m in monitors:
        total_result = await db.execute(
            select(func.count()).select_from(Check)
            .where(Check.monitor_id == m.id, Check.checked_at >= since_90d)
        )
        total = total_result.scalar()

        uptime_pct = 100.0
        if total > 0:
            up_result = await db.execute(
                select(func.count()).select_from(Check)
                .where(
                    Check.monitor_id == m.id,
                    Check.checked_at >= since_90d,
                    Check.status == CheckStatus.UP,
                )
            )
            up_count = up_result.scalar()
            uptime_pct = round((up_count / total) * 100, 2)

        if m.current_status == MonitorStatus.DOWN:
            overall_up = False

        # Get daily uptime for the 90-day bar
        daily_uptimes = await _get_daily_uptimes(m.id, since_90d, now, db)

        monitor_data.append({
            "name": m.name,
            "url": m.url,
            "status": m.current_status.value,
            "uptime_pct": uptime_pct,
            "total_checks": total,
            "daily_uptimes": daily_uptimes,
        })

    # Get recent incidents (last 14 days)
    since_14d = now - timedelta(days=14)
    incidents_result = await db.execute(
        select(Incident, Monitor.name)
        .join(Monitor, Incident.monitor_id == Monitor.id)
        .where(Monitor.project_id == project.id, Incident.started_at >= since_14d)
        .order_by(Incident.started_at.desc())
        .limit(20)
    )
    incidents = [
        {
            "monitor_name": name,
            "started_at": inc.started_at.strftime("%b %d, %Y %H:%M UTC"),
            "resolved_at": inc.resolved_at.strftime("%b %d, %Y %H:%M UTC") if inc.resolved_at else None,
            "duration": _format_duration(int((inc.resolved_at - inc.started_at).total_seconds()))
            if inc.resolved_at else "Ongoing",
        }
        for inc, name in incidents_result.all()
    ]

    overall_status = "operational" if overall_up else "degraded"

    return _render_status_page(status_page, monitor_data, incidents, overall_status)


async def _get_daily_uptimes(
    monitor_id, since: datetime, until: datetime, db: AsyncSession
) -> list[dict]:
    """Get daily uptime percentage for the 90-day bar chart."""
    days = []
    current = since.replace(hour=0, minute=0, second=0, microsecond=0)
    while current < until:
        next_day = current + timedelta(days=1)
        total_result = await db.execute(
            select(func.count()).select_from(Check)
            .where(
                Check.monitor_id == monitor_id,
                Check.checked_at >= current,
                Check.checked_at < next_day,
            )
        )
        total = total_result.scalar()

        if total > 0:
            up_result = await db.execute(
                select(func.count()).select_from(Check)
                .where(
                    Check.monitor_id == monitor_id,
                    Check.checked_at >= current,
                    Check.checked_at < next_day,
                    Check.status == CheckStatus.UP,
                )
            )
            up_count = up_result.scalar()
            pct = round((up_count / total) * 100, 1)
        else:
            pct = None  # no data

        days.append({"date": current.strftime("%Y-%m-%d"), "uptime_pct": pct})
        current = next_day

    return days


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


def _render_status_page(
    status_page: StatusPage,
    monitors: list[dict],
    incidents: list[dict],
    overall_status: str,
) -> str:
    color = status_page.primary_color or "#7C3AED"
    name = status_page.display_name
    logo_html = f'<img src="{status_page.logo_url}" alt="{name}" class="logo">' if status_page.logo_url else ""

    overall_color = "#10B981" if overall_status == "operational" else "#EF4444"
    overall_label = "All Systems Operational" if overall_status == "operational" else "Some Systems Degraded"

    monitors_html = ""
    for m in monitors:
        status_color = "#10B981" if m["status"] == "up" else ("#EF4444" if m["status"] == "down" else "#6B7280")
        status_label = m["status"].upper()

        # Build 90-day bar
        bars = ""
        for day in m["daily_uptimes"]:
            if day["uptime_pct"] is None:
                bar_color = "#E5E7EB"
                tooltip = f'{day["date"]}: No data'
            elif day["uptime_pct"] == 100:
                bar_color = "#10B981"
                tooltip = f'{day["date"]}: 100%'
            elif day["uptime_pct"] >= 99:
                bar_color = "#F59E0B"
                tooltip = f'{day["date"]}: {day["uptime_pct"]}%'
            else:
                bar_color = "#EF4444"
                tooltip = f'{day["date"]}: {day["uptime_pct"]}%'
            bars += f'<div class="bar" style="background:{bar_color}" title="{tooltip}"></div>'

        monitors_html += f"""
        <div class="monitor">
            <div class="monitor-header">
                <div class="monitor-name">
                    <span class="status-dot" style="background:{status_color}"></span>
                    {m['name']}
                </div>
                <div class="monitor-status" style="color:{status_color}">{status_label}</div>
            </div>
            <div class="uptime-bar">{bars}</div>
            <div class="uptime-meta">
                <span>90 days ago</span>
                <span>{m['uptime_pct']}% uptime</span>
                <span>Today</span>
            </div>
        </div>
        """

    incidents_html = ""
    if incidents:
        for inc in incidents:
            resolved = f"Resolved after {inc['duration']}" if inc["resolved_at"] else "🔴 Ongoing"
            incidents_html += f"""
            <div class="incident">
                <div class="incident-title">{inc['monitor_name']}</div>
                <div class="incident-time">{inc['started_at']}</div>
                <div class="incident-status">{resolved}</div>
            </div>
            """
    else:
        incidents_html = '<p class="no-incidents">No incidents in the last 14 days.</p>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name} — Status</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #F9FAFB;
            color: #111827;
            min-height: 100vh;
        }}
        .header {{
            background: {color};
            color: white;
            padding: 2rem 1rem;
            text-align: center;
        }}
        .header .logo {{ height: 48px; margin-bottom: 0.5rem; }}
        .header h1 {{ font-size: 1.5rem; font-weight: 600; }}
        .container {{ max-width: 720px; margin: 0 auto; padding: 1.5rem 1rem; }}
        .overall {{
            background: white;
            border-radius: 8px;
            padding: 1rem 1.5rem;
            margin-bottom: 1.5rem;
            display: flex;
            align-items: center;
            gap: 0.75rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .overall-dot {{
            width: 12px; height: 12px; border-radius: 50%;
            background: {overall_color};
            flex-shrink: 0;
        }}
        .overall-text {{ font-weight: 600; font-size: 1.1rem; }}
        .monitor {{
            background: white;
            border-radius: 8px;
            padding: 1rem 1.5rem;
            margin-bottom: 1rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .monitor-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 0.75rem;
        }}
        .monitor-name {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-weight: 500;
        }}
        .status-dot {{
            width: 10px; height: 10px; border-radius: 50%;
            display: inline-block; flex-shrink: 0;
        }}
        .monitor-status {{ font-size: 0.85rem; font-weight: 600; }}
        .uptime-bar {{
            display: flex;
            gap: 1px;
            height: 28px;
            border-radius: 4px;
            overflow: hidden;
        }}
        .bar {{
            flex: 1;
            min-width: 1px;
            border-radius: 2px;
            cursor: pointer;
            transition: opacity 0.15s;
        }}
        .bar:hover {{ opacity: 0.7; }}
        .uptime-meta {{
            display: flex;
            justify-content: space-between;
            font-size: 0.75rem;
            color: #6B7280;
            margin-top: 0.35rem;
        }}
        .section-title {{
            font-size: 1.1rem;
            font-weight: 600;
            margin: 2rem 0 1rem;
        }}
        .incident {{
            background: white;
            border-radius: 8px;
            padding: 1rem 1.5rem;
            margin-bottom: 0.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        .incident-title {{ font-weight: 500; }}
        .incident-time {{ font-size: 0.85rem; color: #6B7280; }}
        .incident-status {{ font-size: 0.85rem; margin-top: 0.25rem; }}
        .no-incidents {{ color: #6B7280; font-size: 0.9rem; }}
        .footer {{
            text-align: center;
            padding: 2rem 1rem;
            font-size: 0.8rem;
            color: #9CA3AF;
        }}
        .footer a {{ color: {color}; text-decoration: none; }}
    </style>
</head>
<body>
    <div class="header">
        {logo_html}
        <h1>{name}</h1>
    </div>
    <div class="container">
        <div class="overall">
            <div class="overall-dot"></div>
            <div class="overall-text">{overall_label}</div>
        </div>
        {monitors_html}
        <div class="section-title">Recent Incidents</div>
        {incidents_html}
    </div>
    <div class="footer">
        Powered by <a href="#">UptimeBot</a>
    </div>
</body>
</html>"""
