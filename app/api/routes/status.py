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
        status_color = "#10B981" if m["status"] == "up" else ("#EF4444" if m["status"] == "down" else "#64748B")
        status_label = m["status"].upper()

        # Build 90-day bar
        bars = ""
        for day in m["daily_uptimes"]:
            if day["uptime_pct"] is None:
                bar_color = "#1E293B"
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
            resolved_html = (
                f'<span class="incident-badge resolved">Resolved &middot; {inc["duration"]}</span>'
                if inc["resolved_at"]
                else '<span class="incident-badge ongoing">Ongoing</span>'
            )
            incidents_html += f"""
            <div class="incident">
                <div class="incident-head">
                    <div class="incident-title">{inc['monitor_name']}</div>
                    {resolved_html}
                </div>
                <div class="incident-time">{inc['started_at']}</div>
            </div>
            """
    else:
        incidents_html = '<div class="no-incidents"><div class="check-icon">&#10003;</div><p>No incidents in the last 14 days.</p></div>'

    overall_bg = "rgba(16, 185, 129, 0.08)" if overall_status == "operational" else "rgba(239, 68, 68, 0.08)"
    overall_border = "rgba(16, 185, 129, 0.25)" if overall_status == "operational" else "rgba(239, 68, 68, 0.25)"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{name} — Status</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Manrope', system-ui, sans-serif;
            background: #0A0E1A;
            color: #94A3B8;
            min-height: 100vh;
            -webkit-font-smoothing: antialiased;
            line-height: 1.6;
        }}
        body::before {{
            content: '';
            position: fixed;
            inset: 0;
            background-image: radial-gradient(circle at 1px 1px, rgba(148, 163, 184, 0.06) 1px, transparent 0);
            background-size: 32px 32px;
            pointer-events: none;
            z-index: 0;
        }}
        body > * {{ position: relative; z-index: 1; }}
        .font-mono {{ font-family: 'JetBrains Mono', monospace; }}
        .header {{
            padding: 48px 24px 40px;
            text-align: center;
            border-bottom: 1px solid rgba(30, 41, 59, 0.6);
        }}
        .header .logo {{
            max-height: 56px;
            margin: 0 auto 16px;
            display: block;
            border-radius: 6px;
        }}
        .header h1 {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.75rem;
            font-weight: 700;
            color: #F8FAFC;
            letter-spacing: -0.01em;
        }}
        .header .sub {{
            font-size: 0.8125rem;
            color: #64748B;
            font-family: 'JetBrains Mono', monospace;
            margin-top: 8px;
            text-transform: uppercase;
            letter-spacing: 0.1em;
        }}
        .accent-bar {{
            height: 3px;
            background: linear-gradient(90deg, transparent, {color} 30%, {color} 70%, transparent);
            opacity: 0.6;
        }}
        .container {{
            max-width: 760px;
            margin: 0 auto;
            padding: 40px 24px 60px;
        }}
        .overall {{
            background: {overall_bg};
            border: 1px solid {overall_border};
            border-radius: 8px;
            padding: 20px 24px;
            margin-bottom: 40px;
            display: flex;
            align-items: center;
            gap: 14px;
        }}
        .overall-dot {{
            width: 12px; height: 12px; border-radius: 50%;
            background: {overall_color};
            flex-shrink: 0;
            box-shadow: 0 0 12px {overall_color};
            animation: pulse 2.4s ease-in-out infinite;
        }}
        @keyframes pulse {{
            0%, 100% {{ box-shadow: 0 0 8px {overall_color}; }}
            50% {{ box-shadow: 0 0 18px {overall_color}; }}
        }}
        .overall-text {{
            font-family: 'JetBrains Mono', monospace;
            font-weight: 700;
            font-size: 1rem;
            color: {overall_color};
        }}
        .section-label {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.6875rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #64748B;
            margin-bottom: 16px;
        }}
        .monitor {{
            background: rgba(17, 24, 39, 0.7);
            backdrop-filter: blur(8px);
            border: 1px solid rgba(30, 41, 59, 0.8);
            border-radius: 8px;
            padding: 20px 24px;
            margin-bottom: 12px;
            transition: border-color 0.3s ease;
        }}
        .monitor:hover {{ border-color: rgba(124, 58, 237, 0.25); }}
        .monitor-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 14px;
        }}
        .monitor-name {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 500;
            color: #F8FAFC;
            font-size: 0.9375rem;
        }}
        .status-dot {{
            width: 9px; height: 9px; border-radius: 50%;
            display: inline-block; flex-shrink: 0;
        }}
        .monitor-status {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.6875rem;
            font-weight: 600;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }}
        .uptime-bar {{
            display: flex;
            gap: 2px;
            height: 30px;
            border-radius: 3px;
        }}
        .bar {{
            flex: 1;
            min-width: 2px;
            border-radius: 2px;
            cursor: pointer;
            transition: opacity 0.15s, transform 0.15s;
            opacity: 0.75;
        }}
        .bar:hover {{
            opacity: 1;
            transform: scaleY(1.1);
        }}
        .uptime-meta {{
            display: flex;
            justify-content: space-between;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.6875rem;
            color: #64748B;
            margin-top: 10px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}
        .uptime-pct {{ color: #94A3B8; }}
        .section-title {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.6875rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #64748B;
            margin: 40px 0 16px;
        }}
        .incident {{
            background: rgba(17, 24, 39, 0.5);
            border: 1px solid rgba(30, 41, 59, 0.7);
            border-radius: 6px;
            padding: 16px 20px;
            margin-bottom: 8px;
        }}
        .incident-head {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
            gap: 12px;
        }}
        .incident-title {{
            font-weight: 500;
            color: #F8FAFC;
            font-size: 0.875rem;
        }}
        .incident-badge {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.6875rem;
            padding: 3px 10px;
            border-radius: 4px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            flex-shrink: 0;
        }}
        .incident-badge.resolved {{
            background: rgba(16, 185, 129, 0.1);
            color: #10B981;
            border: 1px solid rgba(16, 185, 129, 0.25);
        }}
        .incident-badge.ongoing {{
            background: rgba(239, 68, 68, 0.1);
            color: #EF4444;
            border: 1px solid rgba(239, 68, 68, 0.25);
        }}
        .incident-time {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            color: #64748B;
        }}
        .no-incidents {{
            background: rgba(17, 24, 39, 0.5);
            border: 1px solid rgba(30, 41, 59, 0.7);
            border-radius: 8px;
            padding: 40px 20px;
            text-align: center;
        }}
        .no-incidents .check-icon {{
            width: 40px;
            height: 40px;
            margin: 0 auto 12px;
            border-radius: 50%;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.25);
            color: #10B981;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.125rem;
            font-weight: 700;
        }}
        .no-incidents p {{
            color: #94A3B8;
            font-size: 0.875rem;
        }}
        .footer {{
            text-align: center;
            padding: 32px 24px 40px;
            font-size: 0.75rem;
            color: #475569;
            border-top: 1px solid rgba(30, 41, 59, 0.5);
            margin-top: 40px;
            font-family: 'JetBrains Mono', monospace;
            letter-spacing: 0.05em;
        }}
        .footer a {{
            color: #94A3B8;
            text-decoration: none;
            font-weight: 700;
            transition: color 0.2s;
        }}
        .footer a span {{ color: {color}; }}
        .footer a:hover {{ color: #F8FAFC; }}

        @media (max-width: 640px) {{
            .header {{ padding: 36px 20px 28px; }}
            .header h1 {{ font-size: 1.375rem; }}
            .container {{ padding: 28px 16px 40px; }}
            .monitor, .incident {{ padding: 16px 18px; }}
            .incident-head {{ flex-direction: column; align-items: flex-start; }}
        }}
    </style>
</head>
<body>
    <div class="header">
        {logo_html}
        <h1>{name}</h1>
        <div class="sub">// live system status</div>
    </div>
    <div class="accent-bar"></div>
    <div class="container">
        <div class="overall">
            <div class="overall-dot"></div>
            <div class="overall-text">{overall_label}</div>
        </div>
        <div class="section-label">// Monitors</div>
        {monitors_html}
        <div class="section-title">// Recent Incidents</div>
        {incidents_html}
    </div>
    <div class="footer">
        POWERED BY <a href="/">BotOps<span>Cloud</span></a>
    </div>
</body>
</html>"""
