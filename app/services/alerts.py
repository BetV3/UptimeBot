"""
Alert dispatch service — sends notifications via Discord, Telegram, Email, etc.

Everything here is sync: senders run inside Celery tasks, and the
`enqueue_alerts_sync` insert runs inside the `finalize_check_result`
Celery task guarded by a per-monitor advisory lock. FastAPI routes that
need a one-off send (the test button) call the sync helpers through
`asyncio.to_thread(...)`.
"""

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.models import (
    AlertChannel,
    AlertDelivery,
    AlertDeliveryKind,
    AlertType,
    Incident,
    Monitor,
    MonitorType,
)

RESEND_API_URL = "https://api.resend.com/emails"


def _monitor_target(monitor: Monitor) -> tuple[str, str]:
    """Return (label, value) for the most useful per-type identifier.

    For HTTP monitors the synthetic URL is the real thing being checked.
    For SSL/DNS, `monitor.url` is a synthetic placeholder that wraps the real
    target — host:port for SSL, host (record_type) for DNS — so we surface
    that instead.
    """
    if monitor.type == MonitorType.SSL:
        host = monitor.target_host or ""
        port = monitor.target_port or 443
        return "Host", f"{host}:{port}"
    if monitor.type == MonitorType.DNS:
        host = monitor.target_host or ""
        record_type = monitor.dns_record_type.value if monitor.dns_record_type else "?"
        return "Record", f"{host} ({record_type})"
    return "URL", monitor.url

logger = logging.getLogger(__name__)


def enqueue_alerts_sync(
    session: Session,
    monitor: Monitor,
    incident: Incident,
    event: str,  # "down" or "resolved"
) -> list[str]:
    """Create alert_deliveries rows for every active channel on the project.

    Returns newly inserted delivery IDs (as strings). Rows that collide with
    an existing (incident_id, alert_channel_id, kind) are skipped — that's
    the idempotency guard against double finalize.
    """
    if event not in (AlertDeliveryKind.DOWN.value, AlertDeliveryKind.RESOLVED.value):
        logger.warning(f"enqueue_alerts_sync: unknown event {event!r}, skipping")
        return []

    channel_ids = session.execute(
        select(AlertChannel.id).where(
            AlertChannel.project_id == monitor.project_id,
            AlertChannel.is_active.is_(True),
        )
    ).scalars().all()
    if not channel_ids:
        return []

    rows = [
        {"incident_id": incident.id, "alert_channel_id": cid, "kind": event}
        for cid in channel_ids
    ]
    stmt = (
        pg_insert(AlertDelivery)
        .values(rows)
        .on_conflict_do_nothing(
            index_elements=["incident_id", "alert_channel_id", "kind"]
        )
        .returning(AlertDelivery.id)
    )
    result = session.execute(stmt)
    return [str(row[0]) for row in result.all()]


def send_test_alert_sync(channel: AlertChannel, project_name: str) -> None:
    """Fire a one-off test notification. Runs synchronously; wrap in
    asyncio.to_thread(...) from async code."""
    config = channel.config
    if channel.type == AlertType.DISCORD_WEBHOOK:
        _send_discord_test(config, project_name)
    elif channel.type == AlertType.TELEGRAM:
        _send_telegram_test(config, project_name)
    elif channel.type == AlertType.EMAIL:
        _send_email_test(config, project_name)
    elif channel.type == AlertType.SLACK:
        _send_slack_test(config, project_name)
    elif channel.type == AlertType.WEBHOOK:
        _send_webhook_test(config, project_name)


def send_delivery_sync(
    channel: AlertChannel,
    monitor: Monitor,
    incident: Incident,
    event: str,
    project_name: str,
) -> None:
    """Send one delivery via the channel. Raises on failure so the Celery
    task's retry machinery can back off and try again."""
    config = channel.config
    if channel.type == AlertType.DISCORD_WEBHOOK:
        _send_discord(config, monitor, incident, event, project_name)
    elif channel.type == AlertType.TELEGRAM:
        _send_telegram(config, monitor, incident, event, project_name)
    elif channel.type == AlertType.EMAIL:
        _send_email(config, monitor, incident, event, project_name)
    elif channel.type == AlertType.SLACK:
        _send_slack(config, monitor, incident, event, project_name)
    elif channel.type == AlertType.WEBHOOK:
        _send_webhook(config, monitor, incident, event, project_name)
    else:
        raise ValueError(f"Unsupported alert type: {channel.type!r}")


# --- Discord ---


def _send_discord(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    webhook_url = config["webhook_url"]
    _, target = _monitor_target(monitor)

    if event == "down":
        color = 0xED4245  # red
        title = f"🔴 {monitor.name} is DOWN"
        description = f"Monitor `{target}` is not responding."
        timestamp = incident.started_at.isoformat()
    else:
        color = 0x57F287  # green
        title = f"🟢 {monitor.name} is back UP"
        duration = ""
        if incident.resolved_at and incident.started_at:
            secs = int((incident.resolved_at - incident.started_at).total_seconds())
            duration = f"\nDowntime: {_format_duration(secs)}"
        description = f"Monitor `{target}` has recovered.{duration}"
        timestamp = incident.resolved_at.isoformat() if incident.resolved_at else datetime.now(timezone.utc).isoformat()

    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": f"{project_name} • CheckPulse"},
            "timestamp": timestamp,
        }]
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(webhook_url, json=payload)
        resp.raise_for_status()


def _send_discord_test(config: dict, project_name: str):
    webhook_url = config["webhook_url"]
    payload = {
        "embeds": [{
            "title": "✅ CheckPulse Test Alert",
            "description": "This is a test notification. Your Discord webhook is working!",
            "color": 0x5865F2,
            "footer": {"text": f"{project_name} • CheckPulse"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(webhook_url, json=payload)
        resp.raise_for_status()


# --- Telegram ---


def _send_telegram(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    bot_token = config["bot_token"]
    chat_id = config["chat_id"]
    label, target = _monitor_target(monitor)

    if event == "down":
        text = (
            f"🔴 <b>{monitor.name} is DOWN</b>\n"
            f"{label}: <code>{target}</code>\n"
            f"Project: {project_name}\n"
            f"Time: {incident.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
    else:
        duration = ""
        if incident.resolved_at and incident.started_at:
            secs = int((incident.resolved_at - incident.started_at).total_seconds())
            duration = f"\nDowntime: {_format_duration(secs)}"
        text = (
            f"🟢 <b>{monitor.name} is back UP</b>\n"
            f"{label}: <code>{target}</code>\n"
            f"Project: {project_name}{duration}"
        )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    with httpx.Client(timeout=10) as client:
        resp = client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        resp.raise_for_status()


def _send_telegram_test(config: dict, project_name: str):
    bot_token = config["bot_token"]
    chat_id = config["chat_id"]
    text = f"✅ <b>CheckPulse Test Alert</b>\nYour Telegram notifications are working!\nProject: {project_name}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    with httpx.Client(timeout=10) as client:
        resp = client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        resp.raise_for_status()


# --- Email ---


def _email_uses_smtp(config: dict) -> bool:
    """Channel uses its own SMTP server when smtp_host is configured; otherwise
    we send via the platform's Resend account."""
    return bool((config.get("smtp_host") or "").strip())


def _send_via_resend(to_email: str, subject: str, text: str) -> None:
    """Sync Resend send. In dev (no key, non-prod), log to stdout instead so we
    don't burn credits on local testing."""
    settings = get_settings()
    if settings.app_env != "production" or not settings.resend_api_key:
        logger.warning(
            "\n==================== EMAIL ALERT (dev) ====================\n"
            "To:      %s\nSubject: %s\n%s\n"
            "============================================================",
            to_email, subject, text,
        )
        return

    payload = {
        "from": settings.email_from_address,
        "to": [to_email],
        "subject": subject,
        "text": text,
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(RESEND_API_URL, json=payload, headers=headers)
        resp.raise_for_status()


def _send_email(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    _, target = _monitor_target(monitor)
    if event == "down":
        subject = f"🔴 {monitor.name} is DOWN — {project_name}"
        body = f"Monitor {monitor.name} ({target}) is not responding.\nDetected at: {incident.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    else:
        duration = ""
        if incident.resolved_at and incident.started_at:
            secs = int((incident.resolved_at - incident.started_at).total_seconds())
            duration = f"\nDowntime: {_format_duration(secs)}"
        subject = f"🟢 {monitor.name} is back UP — {project_name}"
        body = f"Monitor {monitor.name} ({target}) has recovered.{duration}"

    if not _email_uses_smtp(config):
        _send_via_resend(config["to_email"], subject, body)
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["from_email"]
    msg["To"] = config["to_email"]

    with smtplib.SMTP(config["smtp_host"], int(config.get("smtp_port", 587))) as server:
        server.starttls()
        server.login(config["smtp_user"], config["smtp_pass"])
        server.send_message(msg)


def _send_email_test(config: dict, project_name: str):
    subject = f"✅ CheckPulse Test Alert — {project_name}"
    body = f"This is a test notification from CheckPulse.\nProject: {project_name}"

    if not _email_uses_smtp(config):
        _send_via_resend(config["to_email"], subject, body)
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["from_email"]
    msg["To"] = config["to_email"]

    with smtplib.SMTP(config["smtp_host"], int(config.get("smtp_port", 587))) as server:
        server.starttls()
        server.login(config["smtp_user"], config["smtp_pass"])
        server.send_message(msg)


# --- Slack ---


def _send_slack(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    webhook_url = config["webhook_url"]
    label, target = _monitor_target(monitor)

    if event == "down":
        color = "#ED4245"
        text = f":red_circle: *{monitor.name}* is DOWN"
        fallback = f"{monitor.name} is DOWN"
    else:
        color = "#57F287"
        duration = ""
        if incident.resolved_at and incident.started_at:
            secs = int((incident.resolved_at - incident.started_at).total_seconds())
            duration = f" (downtime: {_format_duration(secs)})"
        text = f":large_green_circle: *{monitor.name}* is back UP{duration}"
        fallback = f"{monitor.name} is back UP"

    payload = {
        "attachments": [{
            "color": color,
            "fallback": fallback,
            "text": text,
            "fields": [
                {"title": label, "value": f"`{target}`", "short": True},
                {"title": "Project", "value": project_name, "short": True},
            ],
            "footer": "CheckPulse",
            "ts": int(datetime.now(timezone.utc).timestamp()),
        }]
    }

    with httpx.Client(timeout=10) as client:
        resp = client.post(webhook_url, json=payload)
        resp.raise_for_status()


def _send_slack_test(config: dict, project_name: str):
    webhook_url = config["webhook_url"]
    payload = {
        "attachments": [{
            "color": "#5865F2",
            "text": f":white_check_mark: *CheckPulse Test Alert*\nYour Slack webhook is working!\nProject: {project_name}",
            "footer": "CheckPulse",
        }]
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(webhook_url, json=payload)
        resp.raise_for_status()


# --- Generic Webhook ---


def _send_webhook(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    webhook_url = config["url"]
    _, target = _monitor_target(monitor)
    payload = {
        "event": event,
        "monitor": {
            "name": monitor.name,
            "url": monitor.url,
            "type": monitor.type.value,
            "target": target,
        },
        "project": project_name,
        "incident": {
            "started_at": incident.started_at.isoformat(),
            "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else None,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    headers = config.get("headers") or {}
    with httpx.Client(timeout=10) as client:
        resp = client.post(webhook_url, json=payload, headers=headers)
        resp.raise_for_status()


def _send_webhook_test(config: dict, project_name: str):
    webhook_url = config["url"]
    payload = {
        "event": "test",
        "project": project_name,
        "message": "CheckPulse test alert — your webhook is working!",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    headers = config.get("headers") or {}
    with httpx.Client(timeout=10) as client:
        resp = client.post(webhook_url, json=payload, headers=headers)
        resp.raise_for_status()


# --- Helpers ---


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
