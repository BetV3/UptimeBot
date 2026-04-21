"""
Server-rendered dashboard routes.
All form actions POST to these endpoints which call the API layer directly.
Auth is handled via JWT stored in a cookie.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.http import external_base_url, should_secure_cookie
from app.models.models import (
    AlertChannel, AlertType, Check, CheckStatus, Incident,
    Monitor, MonitorStatus, MonitorType, HttpMethod, PlanType, Project, StatusPage, User,
)
from app.services.auth import (
    create_access_token, create_refresh_token,
    decode_token, hash_password, verify_password,
)
from app.services.email import (
    generate_password_reset_token,
    generate_verification_token,
    send_password_reset_email,
    send_verification_email,
)
from app.services.plans import PLAN_LIMITS, check_project_limit, check_monitor_limit, check_interval_limit

router = APIRouter()
settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent.parent / "templates"))


# --- Auth helpers ---

async def _get_user_from_cookie(request: Request, db: AsyncSession) -> User | None:
    """
    Return the cookie-authenticated user, or None if the cookie is missing,
    invalid, or the account has not completed email verification yet.
    """
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        user_id = decode_token(token, expected_type="access")
    except HTTPException:
        return None
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if settings.app_env == "production" and not user.email_verified:
        return None
    return user


def _set_token_cookie(response: RedirectResponse, token: str):
    response.set_cookie(
        "access_token",
        token,
        httponly=True,
        max_age=60 * 60 * 24 * 7,
        path="/",
        samesite="lax",
        secure=should_secure_cookie(),
    )


def _redirect_with_flash(url: str, message: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        "flash_error",
        message,
        max_age=30,
        path="/",
        samesite="lax",
        secure=should_secure_cookie(),
    )
    return response


def _consume_flash(request: Request, response) -> str | None:
    msg = request.cookies.get("flash_error")
    if msg:
        response.delete_cookie("flash_error", path="/")
    return msg


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


MONITOR_NAME_MAX = 100
PROJECT_NAME_MAX = 80


def _parse_int_field(raw: str, label: str, min_value: int = 1, max_value: int | None = None) -> int:
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail=f"{label} is required.")
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label} must be a whole number.")
    if value < min_value:
        raise HTTPException(status_code=400, detail=f"{label} must be at least {min_value}.")
    if max_value is not None and value > max_value:
        raise HTTPException(status_code=400, detail=f"{label} must be {max_value} or less.")
    return value


def _validate_monitor_fields(name: str, url: str, interval: int, timeout: int, method: str) -> tuple[str, str, HttpMethod]:
    name = (name or "").strip()
    url = (url or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Monitor name can't be blank.")
    if len(name) > MONITOR_NAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Monitor name is too long — keep it to {MONITOR_NAME_MAX} characters or fewer.",
        )
    if not url:
        raise HTTPException(status_code=400, detail="URL can't be blank.")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://.",
        )
    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=f"“{url}” isn't a valid URL. Try something like https://example.com/health.",
        )

    if timeout >= interval:
        raise HTTPException(
            status_code=400,
            detail="Timeout must be less than the check interval.",
        )
    if timeout > 60:
        raise HTTPException(status_code=400, detail="Timeout can't exceed 60 seconds.")

    try:
        http_method = HttpMethod(method)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"“{method}” isn't a supported HTTP method.")

    return name, url, http_method


def _validate_ssl_fields(
    name: str, host: str, port: int, warn_days: int | None, interval: int, timeout: int
) -> tuple[str, str, int, int | None]:
    name = (name or "").strip()
    host = (host or "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Monitor name can't be blank.")
    if len(name) > MONITOR_NAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Monitor name is too long — keep it to {MONITOR_NAME_MAX} characters or fewer.",
        )
    if not host:
        raise HTTPException(status_code=400, detail="Target host can't be blank.")
    if "://" in host or "/" in host:
        raise HTTPException(
            status_code=400,
            detail="Target host should be a hostname like example.com — no scheme or path.",
        )
    if "." not in host:
        raise HTTPException(
            status_code=400,
            detail=f"“{host}” isn't a valid hostname.",
        )
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1 and 65535.")
    if warn_days is not None and warn_days < 0:
        raise HTTPException(status_code=400, detail="Warn days can't be negative.")
    if timeout >= interval:
        raise HTTPException(status_code=400, detail="Timeout must be less than the check interval.")
    if timeout > 60:
        raise HTTPException(status_code=400, detail="Timeout can't exceed 60 seconds.")

    return name, host, port, warn_days


async def _check_duplicate_monitor_name(
    db: AsyncSession, project_id: str, name: str, exclude_id: str | None = None
) -> None:
    stmt = select(Monitor).where(Monitor.project_id == project_id, Monitor.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Monitor.id != exclude_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A monitor named “{name}” already exists in this project.",
        )


# --- Auth pages ---

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if await _get_user_from_cookie(request, db):
        return RedirectResponse("/dashboard", status_code=303)
    notice = request.cookies.get("flash_notice")
    response = templates.TemplateResponse(
        "login.html", {"request": request, "error": None, "notice": notice}
    )
    if notice:
        response.delete_cookie("flash_notice", path="/")
    return response


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

    if settings.app_env == "production" and not user.email_verified:
        return templates.TemplateResponse(
            "check_email.html",
            {
                "request": request,
                "email": user.email,
                "info": "Please verify your email before signing in.",
            },
            status_code=403,
        )

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
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if password != confirm_password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Passwords do not match"}, status_code=400)

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Email already registered"}, status_code=409)

    is_production = settings.app_env == "production"

    if is_production:
        token, expires_at = generate_verification_token()
        user = User(
            email=email,
            password_hash=hash_password(password),
            email_verified=False,
            verification_token=token,
            verification_token_expires_at=expires_at,
        )
    else:
        user = User(
            email=email,
            password_hash=hash_password(password),
            email_verified=True,
        )

    db.add(user)
    await db.commit()
    await db.refresh(user)

    if is_production:
        await send_verification_email(user.email, token, base_url=external_base_url(request))
        return templates.TemplateResponse(
            "check_email.html",
            {"request": request, "email": user.email, "info": None},
        )

    # Dev mode: log in immediately
    response = RedirectResponse("/dashboard", status_code=303)
    _set_token_cookie(response, create_access_token(str(user.id)))
    return response


@router.get("/verify", response_class=HTMLResponse)
async def verify_email(
    request: Request,
    token: str = "",
    db: AsyncSession = Depends(get_db),
):
    if not token:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Missing verification token"},
            status_code=400,
        )

    result = await db.execute(select(User).where(User.verification_token == token))
    user = result.scalar_one_or_none()
    if user is None:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid or expired verification link"},
            status_code=400,
        )

    expires_at = user.verification_token_expires_at
    now = datetime.now(timezone.utc)
    if expires_at is not None and expires_at < now:
        return templates.TemplateResponse(
            "check_email.html",
            {
                "request": request,
                "email": user.email,
                "info": "That link expired. We can send you a new one.",
            },
            status_code=400,
        )

    user.email_verified = True
    user.verification_token = None
    user.verification_token_expires_at = None
    await db.commit()

    response = RedirectResponse("/dashboard", status_code=303)
    _set_token_cookie(response, create_access_token(str(user.id)))
    return response


@router.post("/resend-verification")
async def resend_verification(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Always return the same confirmation page so this endpoint can't be used
    # to enumerate which emails are registered.
    if user is not None and not user.email_verified:
        token, expires_at = generate_verification_token()
        user.verification_token = token
        user.verification_token_expires_at = expires_at
        await db.commit()
        await send_verification_email(user.email, token, base_url=external_base_url(request))

    return templates.TemplateResponse(
        "check_email.html",
        {
            "request": request,
            "email": email,
            "info": "If that account exists and is unverified, a new link is on its way.",
        },
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie("access_token", path="/", samesite="lax", secure=should_secure_cookie())
    return response


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)
    response = templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "password_error": None,
            "flash_error": request.cookies.get("flash_error"),
            "flash_notice": request.cookies.get("flash_notice"),
        },
    )
    _consume_flash(request, response)
    if request.cookies.get("flash_notice"):
        response.delete_cookie("flash_notice", path="/")
    return response


@router.post("/account/password")
async def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    def render(err: str, code: int = 400):
        return templates.TemplateResponse(
            "account.html",
            {
                "request": request,
                "user": user,
                "password_error": err,
                "flash_error": None,
                "flash_notice": None,
            },
            status_code=code,
        )

    if not verify_password(current_password, user.password_hash):
        return render("Current password is incorrect.")
    if len(new_password) < 8:
        return render("New password must be at least 8 characters.")
    if new_password != confirm_password:
        return render("New passwords do not match.")
    if new_password == current_password:
        return render("New password must differ from current password.")

    user.password_hash = hash_password(new_password)
    await db.commit()

    response = RedirectResponse("/dashboard/account", status_code=303)
    response.set_cookie(
        "flash_notice",
        "Password updated.",
        max_age=30,
        path="/",
        samesite="lax",
        secure=should_secure_cookie(),
    )
    return response


@router.get("/billing", response_class=HTMLResponse)
async def billing_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    # Usage counts for the "current usage" card
    project_count = (
        await db.execute(
            select(func.count()).select_from(Project).where(Project.user_id == user.id)
        )
    ).scalar() or 0
    monitor_count = (
        await db.execute(
            select(func.count())
            .select_from(Monitor)
            .join(Project, Monitor.project_id == Project.id)
            .where(Project.user_id == user.id)
        )
    ).scalar() or 0

    upgraded = request.query_params.get("upgraded") == "1"
    cancelled = request.query_params.get("cancelled") == "1"

    flash_notice = request.cookies.get("flash_notice")
    flash_error = request.cookies.get("flash_error")
    if upgraded:
        flash_notice = flash_notice or "Subscription activated. Welcome aboard!"
    if cancelled:
        flash_error = flash_error or "Checkout cancelled - you weren't charged."

    tiers = [
        {
            "key": "free",
            "name": "Free",
            "price": "$0",
            "tagline": "For side projects and hobby bots.",
            "features": [
                f"{PLAN_LIMITS[PlanType.FREE].max_projects} bot",
                f"{PLAN_LIMITS[PlanType.FREE].max_monitors} monitors",
                f"{PLAN_LIMITS[PlanType.FREE].min_interval_seconds // 60}-minute intervals",
                "Email + Discord alerts",
                "Basic trust page",
            ],
        },
        {
            "key": "starter",
            "name": "Starter",
            "price": "$12",
            "tagline": "For developers who take uptime seriously.",
            "features": [
                f"{PLAN_LIMITS[PlanType.STARTER].max_projects} bots",
                f"{PLAN_LIMITS[PlanType.STARTER].max_monitors} monitors",
                f"{PLAN_LIMITS[PlanType.STARTER].min_interval_seconds}-second intervals",
                "Slack + Telegram + webhook alerts",
                "Custom domain on trust pages",
                "90-day history",
            ],
            "featured": True,
        },
        {
            "key": "pro",
            "name": "Pro",
            "price": "$49",
            "tagline": "For teams running multiple production bots.",
            "features": [
                "Unlimited bots",
                f"{PLAN_LIMITS[PlanType.PRO].max_monitors} monitors",
                f"{PLAN_LIMITS[PlanType.PRO].min_interval_seconds}-second intervals",
                "Branded trust pages",
                "Priority support",
                "1-year history",
            ],
        },
    ]

    response = templates.TemplateResponse(
        "billing.html",
        {
            "request": request,
            "user": user,
            "project_count": project_count,
            "monitor_count": monitor_count,
            "current_plan": user.plan.value,
            "current_limits": PLAN_LIMITS[user.plan],
            "tiers": tiers,
            "subscription_status": user.subscription_status.value if user.subscription_status else None,
            "current_period_end": user.current_period_end,
            "has_stripe_customer": bool(user.stripe_customer_id),
            "stripe_configured": bool(settings.stripe_secret_key),
            "flash_notice": flash_notice,
            "flash_error": flash_error,
        },
    )
    _consume_flash(request, response)
    if request.cookies.get("flash_notice"):
        response.delete_cookie("flash_notice", path="/")
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "sent": False, "email": None, "error": None},
    )


@router.post("/forgot-password")
async def forgot_password_submit(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    email = (email or "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Always show the same confirmation to avoid email enumeration.
    if user is not None and user.email_verified:
        token, expires_at = generate_password_reset_token()
        user.password_reset_token = token
        user.password_reset_expires_at = expires_at
        await db.commit()
        await send_password_reset_email(user.email, token, base_url=external_base_url(request))

    return templates.TemplateResponse(
        "forgot_password.html",
        {"request": request, "sent": True, "email": email, "error": None},
    )


@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(
    request: Request,
    token: str = "",
    db: AsyncSession = Depends(get_db),
):
    if not token:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": "", "invalid": True, "error": None},
            status_code=400,
        )
    result = await db.execute(select(User).where(User.password_reset_token == token))
    user = result.scalar_one_or_none()
    invalid = (
        user is None
        or user.password_reset_expires_at is None
        or user.password_reset_expires_at < datetime.now(timezone.utc)
    )
    return templates.TemplateResponse(
        "reset_password.html",
        {"request": request, "token": token, "invalid": invalid, "error": None},
        status_code=400 if invalid else 200,
    )


@router.post("/reset-password")
async def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if len(password) < 8:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "invalid": False, "error": "Password must be at least 8 characters."},
            status_code=400,
        )
    if password != confirm_password:
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "invalid": False, "error": "Passwords do not match."},
            status_code=400,
        )

    result = await db.execute(select(User).where(User.password_reset_token == token))
    user = result.scalar_one_or_none()
    if (
        user is None
        or user.password_reset_expires_at is None
        or user.password_reset_expires_at < datetime.now(timezone.utc)
    ):
        return templates.TemplateResponse(
            "reset_password.html",
            {"request": request, "token": token, "invalid": True, "error": None},
            status_code=400,
        )

    user.password_hash = hash_password(password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    await db.commit()

    response = RedirectResponse("/dashboard/login", status_code=303)
    response.set_cookie(
        "flash_notice",
        "Password updated - sign in with your new password.",
        max_age=30,
        path="/",
        samesite="lax",
        secure=should_secure_cookie(),
    )
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

    response = templates.TemplateResponse("projects.html", {
        "request": request, "user": user,
        "projects": [{"id": str(p.id), "name": p.name, "slug": p.slug} for p in projects],
        "flash_error": request.cookies.get("flash_error"),
    })
    _consume_flash(request, response)
    return response


@router.post("/projects")
async def create_project_submit(
    request: Request,
    name: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    import re, uuid as _uuid
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    name_clean = (name or "").strip()
    try:
        if not name_clean:
            raise HTTPException(status_code=400, detail="Project name can't be blank.")
        if len(name_clean) > PROJECT_NAME_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"Project name is too long — keep it to {PROJECT_NAME_MAX} characters or fewer.",
            )
        dup = await db.execute(
            select(Project).where(Project.user_id == user.id, Project.name == name_clean)
        )
        if dup.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"You already have a project named “{name_clean}”.",
            )
        await check_project_limit(user, db)
    except HTTPException as e:
        if _wants_json(request):
            raise
        return _redirect_with_flash("/dashboard", e.detail)

    slug = re.sub(r"[^\w\s-]", "", name_clean.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-") or _uuid.uuid4().hex[:8]
    existing = await db.execute(select(Project).where(Project.slug == slug))
    if existing.scalar_one_or_none():
        slug = f"{slug}-{_uuid.uuid4().hex[:6]}"

    project = Project(user_id=user.id, name=name_clean, slug=slug)
    db.add(project)
    await db.flush()
    sp = StatusPage(project_id=project.id, display_name=name_clean, is_public=False)
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

    response = templates.TemplateResponse("project_detail.html", {
        "request": request, "user": user,
        "project": {"id": str(project.id), "name": project.name, "slug": project.slug},
        "monitors": [
            {
                "id": str(m.id), "name": m.name, "url": m.url,
                "type": m.type.value,
                "target_host": m.target_host, "target_port": m.target_port,
                "current_status": m.current_status.value, "is_active": m.is_active,
            }
            for m in monitors
        ],
        "alerts": [
            {"id": str(a.id), "type": a.type.value, "is_active": a.is_active}
            for a in alerts
        ],
        "incidents": incidents,
        "flash_error": request.cookies.get("flash_error"),
        "flash_notice": request.cookies.get("flash_notice"),
        "min_interval_seconds": PLAN_LIMITS[user.plan].min_interval_seconds,
    })
    _consume_flash(request, response)
    if request.cookies.get("flash_notice"):
        response.delete_cookie("flash_notice", path="/")
    return response


@router.post("/projects/{project_id}/monitors")
async def create_monitor_submit(
    project_id: str, request: Request,
    name: str = Form(""),
    type: str = Form("http"),
    url: str = Form(""),
    method: str = Form("GET"),
    expected_status: str = Form("200"),
    target_host: str = Form(""),
    target_port: str = Form("443"),
    warn_days_before_expiry: str = Form("14"),
    interval_seconds: str = Form(""),
    timeout_seconds: str = Form(""),
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

    try:
        try:
            monitor_type = MonitorType(type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"“{type}” isn't a supported monitor type.")

        interval = _parse_int_field(interval_seconds, "Check interval")
        timeout = _parse_int_field(timeout_seconds, "Timeout")

        if monitor_type == MonitorType.HTTP:
            expected = _parse_int_field(expected_status, "Expected status", min_value=100, max_value=599)
            name_clean, url_clean, http_method = _validate_monitor_fields(
                name, url, interval, timeout, method
            )
            host_clean, port_clean, warn_clean = None, None, None
        else:
            port = _parse_int_field(target_port, "Port", min_value=1, max_value=65535)
            warn_raw = (warn_days_before_expiry or "").strip()
            warn = _parse_int_field(warn_days_before_expiry, "Warn days", min_value=0, max_value=365) if warn_raw else None
            name_clean, host_clean, port_clean, warn_clean = _validate_ssl_fields(
                name, target_host, port, warn, interval, timeout
            )
            url_clean = f"https://{host_clean}:{port_clean}"
            http_method = HttpMethod.GET
            expected = 200

        await _check_duplicate_monitor_name(db, project_id, name_clean)
        await check_monitor_limit(user, db)
        check_interval_limit(user, interval)
    except HTTPException as e:
        if _wants_json(request):
            raise
        return _redirect_with_flash(f"/dashboard/projects/{project_id}", e.detail)

    monitor = Monitor(
        project_id=project_id, name=name_clean, type=monitor_type, url=url_clean,
        method=http_method, expected_status=expected,
        interval_seconds=interval, timeout_seconds=timeout,
        target_host=host_clean, target_port=port_clean,
        warn_days_before_expiry=warn_clean,
        next_check_at=func.now(),
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
    slack_webhook_url: str = Form(""),
    generic_webhook_url: str = Form(""),
    webhook_secret: str = Form(""),
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
    elif type == "slack":
        config = {"webhook_url": slack_webhook_url}
    elif type == "webhook":
        config = {"url": generic_webhook_url}
        if webhook_secret:
            config["headers"] = {"Authorization": webhook_secret}

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


@router.post("/alerts/{channel_id}/test")
async def test_alert_submit(channel_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    import asyncio

    from app.services.alerts import send_test_alert_sync

    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)

    result = await db.execute(
        select(AlertChannel, Project.name)
        .join(Project, AlertChannel.project_id == Project.id)
        .where(AlertChannel.id == channel_id, Project.user_id == user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404)
    channel, project_name = row

    project_id = str(channel.project_id)
    try:
        await asyncio.to_thread(send_test_alert_sync, channel, project_name)
    except Exception as e:
        return _redirect_with_flash(
            f"/dashboard/projects/{project_id}",
            f"Test alert failed: {str(e)[:200]}",
        )

    response = RedirectResponse(f"/dashboard/projects/{project_id}", status_code=303)
    response.set_cookie("flash_notice", "Test alert sent.", max_age=30, path="/", samesite="lax")
    return response


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
        select(Check).where(Check.monitor_id == monitor.id).order_by(Check.checked_at.desc()).limit(20)
    )
    check_rows = checks_result.scalars().all()
    checks = [
        {
            "checked_at": c.checked_at.isoformat(),
            "region": c.region.value,
            "status": c.status.value,
            "status_code": c.status_code,
            "response_time_ms": c.response_time_ms,
            "cert_days_remaining": c.cert_days_remaining,
        }
        for c in check_rows
    ]

    latest_cert = None
    if monitor.type == MonitorType.SSL:
        for c in check_rows:
            if c.cert_days_remaining is not None or c.cert_subject or c.cert_issuer:
                latest_cert = {
                    "days_remaining": c.cert_days_remaining,
                    "subject": c.cert_subject,
                    "issuer": c.cert_issuer,
                    "checked_at": c.checked_at.isoformat(),
                }
                break

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

    response = templates.TemplateResponse("monitor_detail.html", {
        "request": request, "user": user,
        "monitor": {
            "id": str(monitor.id), "project_id": str(monitor.project_id),
            "name": monitor.name, "type": monitor.type.value,
            "url": monitor.url, "method": monitor.method.value,
            "expected_status": monitor.expected_status,
            "target_host": monitor.target_host,
            "target_port": monitor.target_port,
            "warn_days_before_expiry": monitor.warn_days_before_expiry,
            "current_status": monitor.current_status.value, "is_active": monitor.is_active,
            "interval_seconds": monitor.interval_seconds,
            "timeout_seconds": monitor.timeout_seconds,
        },
        "summary": summary, "checks": checks, "chart_data": chart_data,
        "latest_cert": latest_cert,
        "flash_error": request.cookies.get("flash_error"),
    })
    _consume_flash(request, response)
    return response


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
    monitor.next_check_at = func.now()
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


@router.post("/monitors/{monitor_id}/edit")
async def edit_monitor_submit(
    monitor_id: str, request: Request,
    name: str = Form(""),
    url: str = Form(""),
    method: str = Form("GET"),
    expected_status: str = Form("200"),
    target_host: str = Form(""),
    target_port: str = Form("443"),
    warn_days_before_expiry: str = Form("14"),
    interval_seconds: str = Form(""),
    timeout_seconds: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_from_cookie(request, db)
    if not user:
        return RedirectResponse("/dashboard/login", status_code=303)
    result = await db.execute(
        select(Monitor).join(Project).where(Monitor.id == monitor_id, Project.user_id == user.id)
    )
    monitor = result.scalar_one_or_none()
    if not monitor:
        raise HTTPException(status_code=404)

    try:
        interval = _parse_int_field(interval_seconds, "Check interval")
        timeout = _parse_int_field(timeout_seconds, "Timeout")

        if monitor.type == MonitorType.HTTP:
            expected = _parse_int_field(expected_status, "Expected status", min_value=100, max_value=599)
            name_clean, url_clean, http_method = _validate_monitor_fields(
                name, url, interval, timeout, method
            )
            host_clean, port_clean, warn_clean = None, None, None
        else:
            port = _parse_int_field(target_port, "Port", min_value=1, max_value=65535)
            warn_raw = (warn_days_before_expiry or "").strip()
            warn = _parse_int_field(warn_days_before_expiry, "Warn days", min_value=0, max_value=365) if warn_raw else None
            name_clean, host_clean, port_clean, warn_clean = _validate_ssl_fields(
                name, target_host, port, warn, interval, timeout
            )
            url_clean = f"https://{host_clean}:{port_clean}"
            http_method = monitor.method
            expected = monitor.expected_status

        await _check_duplicate_monitor_name(
            db, str(monitor.project_id), name_clean, exclude_id=str(monitor.id)
        )
        check_interval_limit(user, interval)
    except HTTPException as e:
        if _wants_json(request):
            raise
        return _redirect_with_flash(f"/dashboard/monitors/{monitor_id}", e.detail)

    monitor.name = name_clean
    monitor.url = url_clean
    monitor.method = http_method
    monitor.expected_status = expected
    monitor.interval_seconds = interval
    monitor.timeout_seconds = timeout
    monitor.target_host = host_clean
    monitor.target_port = port_clean
    monitor.warn_days_before_expiry = warn_clean
    await db.commit()
    return RedirectResponse(f"/dashboard/monitors/{monitor_id}", status_code=303)


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
