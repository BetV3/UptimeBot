"""
Server-rendered dashboard routes.
All form actions POST to these endpoints which call the API layer directly.
Auth is handled via JWT stored in a cookie.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import (
    AlertChannel, AlertType, Check, CheckStatus, Incident,
    Monitor, MonitorStatus, HttpMethod, Project, StatusPage, User,
)
from app.services.auth import (
    create_access_token, create_refresh_token,
    decode_token, hash_password, verify_password,
)

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent.parent / "templates"))


# --- Auth helpers ---

async def _get_user_from_cookie(request: Request, db: AsyncSession) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        user_id = decode_token(token, expected_type="access")
    except HTTPException:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def _set_token_cookie(response: RedirectResponse, token: str):
    response.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)


# --- Auth pages ---

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"}, status_code=401)

    response = RedirectResponse("/dashboard", status_code=303)
    _set_token_cookie(response, create_access_token(str(user.id)))
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request, "error": None})


@router.post("/register")
async def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered"}, status_code=409)

    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    response = RedirectResponse("/dashboard", status_code=303)
    _set_token_cookie(response, create_access_token(str(user.id)))
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie("access_token")
    return response


# --- Dashboard pages ---

@router.get("", response_class=HTMLResponse)
async def dashboard_home(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()

    return templates.TemplateResponse("projects.html", {
        "request": request, "user": user,
        "projects": [{"id": str(p.id), "name": p.name, "slug": p.slug} for p in projects],
    })


@router.post("/projects")
async def create_project_submit(
    request: Request,
    name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    import re, uuid as _uuid
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    slug = re.sub(r"[^\w\s-]", "", name.lower().strip())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    existing = await db.execute(select(Project).where(Project.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{_uuid.uuid4().hex[:6]}"

    project = Project(user_id=user.id, name=name, slug=slug)
    db.add(project)
    await db.flush()
    sp = StatusPage(project_id=project.id, display_name=name, is_public=False)
    db.add(sp)
    await db.commit()

    return RedirectResponse(f"/dashboard/projects/{project.id}", status_code=303)


# --- Project detail ---

@router.get("/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Monitors
    monitors_result = await db.execute(
        select(Monitor).where(Monitor.project_id == project.id).order_by(Monitor.created_at.desc())
    )
    monitors = monitors_result.scalars().all()

    # Alerts
    alerts_result = await db.execute(
        select(AlertChannel).where(AlertChannel.project_id == project.id).order_by(AlertChannel.created_at.desc())
    )
    alerts = alerts_result.scalars().all()

    # Incidents
    incidents_result = await db.execute(
        select(Incident, Monitor.name)
        .join(Monitor, Incident.monitor_id == Monitor.id)
        .where(Monitor.project_id == project.id)
        .order_by(Incident.started_at.desc())
        .limit(20)
    )

    incidents = [
        {
            "monitor_name": name,
            "started_at": inc.started_at.strftime("%b %d %H:%M UTC"),
            "resolved_at": inc.resolved_at.strftime("%b %d %H:%M UTC") if inc.resolved_at else None,
            "duration_seconds": int((inc.resolved_at - inc.started_at).total_seconds()) if inc.resolved_at else None,
        }
        for inc, name in incidents_result.all()
    ]

    return templates.TemplateResponse("project_detail.html", {
        "request": request, "user": user,
        "project": {"id": str(project.id), "name": project.name, "slug": project.slug},
        "monitors": [
            {"id": str(m.id), "name": m.name, "url": m.url, "current_status": m.current_status.value, "is_active": m.is_active}
            for m in monitors
        ],
        "alerts": [
            {"id": str(a.id), "type": a.type.value, "is_active": a.is_active}
            for a in alerts
        ],
        "incidents": incidents,
    })


@router.post("/projects/{project_id}/monitors")
async def create_monitor_submit(
    project_id: str, request: Request,
    name: str = Form(...),
    url: str = Form(...),
    method: str = Form("GET"),
    expected_status: int = Form(200),
    interval_seconds: int = Form(60),
    timeout_seconds: int = Form(10),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404)

    monitor = Monitor(
        project_id=project_id, name=name, url=url,
        method=HttpMethod(method), expected_status=expected_status,
        interval_seconds=interval_seconds, timeout_seconds=timeout_seconds,
    )
    db.add(monitor)
    await db.commit()
    return RedirectResponse(f"/dashboard/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/alerts")
async def create_alert_submit(
    project_id: str, request: Request,
    type: str = Form(...),
    webhook_url: str = Form(""),
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_user: str = Form(""),
    smtp_pass: str = Form(""),
    from_email: str = Form(""),
    to_email: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404)

    config = {}
    if type == "discord_webhook":
        config = {"webhook_url": webhook_url}
    elif type == "telegram":
        config = {"bot_token": bot_token, "chat_id": chat_id}
    elif type == "email":
        config = {
            "smtp_host": smtp_host, "smtp_port": smtp_port,
            "smtp_user": smtp_user, "smtp_pass": smtp_pass,
            "from_email": from_email, "to_email": to_email,
        }

    channel = AlertChannel(project_id=project_id, type=AlertType(type), config=config)
    db.add(channel)
    await db.commit()
    return RedirectResponse(f"/dashboard/projects/{project_id}", status_code=303)


@router.post("/alerts/{channel_id}/delete")
async def delete_alert_submit(channel_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(AlertChannel)
        .join(Project, AlertChannel.project_id == Project.id)
        .where(AlertChannel.id == channel_id, Project.user_id == user.id)
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=404)

    project_id = str(channel.project_id)
    await db.delete(channel)
    await db.commit()
    return RedirectResponse(f"/dashboard/projects/{project_id}", status_code=303)


# --- Monitor detail ---

@router.get("/monitors/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail_page(monitor_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(Monitor)
        .join(Project, Monitor.project_id == Project.id)
        .where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404)

    # Uptime summary
    now = datetime.now(timezone.utc)
    summary = {}
    for label, delta in [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30)), ("90d", timedelta(days=90))]:
        since = now - delta
        total_r = await db.execute(select(func.count()).select_from(Check).where(Check.monitor_id == monitor.id, Check.checked_at >= since))
        total = total_r.scalar()
        if total == 0:
            summary[label] = {"uptime_pct": None, "total_checks": 0}
        else:
            up_r = await db.execute(select(func.count()).select_from(Check).where(Check.monitor_id == monitor.id, Check.checked_at >= since, Check.status == CheckStatus.UP))
            summary[label] = {"uptime_pct": round((up_r.scalar() / total) * 100, 2), "total_checks": total}

    # Recent checks
    checks_result = await db.execute(
        select(Check).where(Check.monitor_id == monitor.id).order_by(Check.checked_at.desc()).limit(50)
    )
    checks = [
        {
            "checked_at": c.checked_at.isoformat(),
            "region": c.region.value,
            "status": c.status.value,
            "status_code": c.status_code,
            "response_time_ms": c.response_time_ms,
        }
        for c in checks_result.scalars().all()
    ]

    # Chart data — last 24h of checks with response times
    since_24h = now - timedelta(hours=24)
    chart_result = await db.execute(
        select(Check)
        .where(Check.monitor_id == monitor.id, Check.checked_at >= since_24h)
        .order_by(Check.checked_at.asc())
    )
    chart_data = [
        {"time": c.checked_at.strftime("%H:%M"), "ms": c.response_time_ms}
        for c in chart_result.scalars().all()
    ]

    return templates.TemplateResponse("monitor_detail.html", {
        "request": request, "user": user,
        "monitor": {
            "id": str(monitor.id), "project_id": str(monitor.project_id),
            "name": monitor.name, "url": monitor.url, "method": monitor.method.value,
            "current_status": monitor.current_status.value, "is_active": monitor.is_active,
            "interval_seconds": monitor.interval_seconds,
        },
        "summary": summary, "checks": checks, "chart_data": chart_data,
    })


@router.post("/monitors/{monitor_id}/pause")
async def pause_monitor_submit(monitor_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)
    result = await db.execute(
        select(Monitor).join(Project).where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404)
    monitor.is_active = False
    await db.commit()
    return RedirectResponse(f"/dashboard/monitors/{monitor_id}", status_code=303)


@router.post("/monitors/{monitor_id}/resume")
async def resume_monitor_submit(monitor_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)
    result = await db.execute(
        select(Monitor).join(Project).where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404)
    monitor.is_active = True
    await db.commit()
    return RedirectResponse(f"/dashboard/monitors/{monitor_id}", status_code=303)


@router.post("/monitors/{monitor_id}/delete")
async def delete_monitor_submit(monitor_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)
    result = await db.execute(
        select(Monitor).join(Project).where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404)
    project_id = str(monitor.project_id)
    await db.delete(monitor)
    await db.commit()
    return RedirectResponse(f"/dashboard/projects/{project_id}", status_code=303)


# --- Status page settings ---

@router.get("/projects/{project_id}/status-page", response_class=HTMLResponse)
async def status_page_settings(project_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(Project, StatusPage)
        .join(StatusPage, StatusPage.project_id == Project.id)
        .where(Project.id == project_id, Project.user_id == user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404)

    project, sp = row
    return templates.TemplateResponse("status_page_settings.html", {
        "request": request, "user": user,
        "project": {"id": str(project.id), "name": project.name, "slug": project.slug},
        "status_page": {
            "is_public": sp.is_public, "display_name": sp.display_name,
            "logo_url": sp.logo_url or "", "primary_color": sp.primary_color,
        },
    })


@router.post("/projects/{project_id}/status-page")
async def status_page_settings_submit(
    project_id: str, request: Request,
    display_name: str = Form(...),
    primary_color: str = Form("#7C3AED"),
    logo_url: str = Form(""),
    is_public: str = Form("off"),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(StatusPage)
        .join(Project, StatusPage.project_id == Project.id)
        .where(Project.id == project_id, Project.user_id == user.id)
    )
    sp = result.scalar_one_or_none()
    if not sp:
        raise HTTPException(status_code=404)

    sp.display_name = display_name
    sp.primary_color = primary_color
    sp.logo_url = logo_url or None
    sp.is_public = is_public == "on"
    await db.commit()
    return RedirectResponse(f"/dashboard/projects/{project_id}/status-page", status_code=303)
