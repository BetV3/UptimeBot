"""
Alert dispatch service — sends notifications via Discord, Telegram, and Email.
"""

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import AlertChannel, AlertType, Monitor, Incident, Project

logger = logging.getLogger(__name__)


async def dispatch_alerts(
    monitor: Monitor,
    incident: Incident,
    event: str,  # "down" or "resolved"
    db: AsyncSession,
):
    """Send alerts to all active channels for the monitor's project."""
    result = await db.execute(
        select(AlertChannel).where(
            AlertChannel.project_id == monitor.project_id,
            AlertChannel.is_active.is_(True),
        )
    )
    channels = result.scalars().all()

    # Get project name for context
    proj_result = await db.execute(
        select(Project.name).where(Project.id == monitor.project_id)
    )
    project_name = proj_result.scalar() or "Unknown"

    for channel in channels:
        try:
            await _send_alert(channel, monitor, incident, event, project_name)
        except Exception:
            logger.exception(f"Failed to send alert via {channel.type.value} (channel {channel.id})")


async def _send_alert(
    channel: AlertChannel,
    monitor: Monitor,
    incident: Incident,
    event: str,
    project_name: str,
):
    if channel.type == AlertType.DISCORD_WEBHOOK:
        await _send_discord(channel.config, monitor, incident, event, project_name)
    elif channel.type == AlertType.TELEGRAM:
        await _send_telegram(channel.config, monitor, incident, event, project_name)
    elif channel.type == AlertType.EMAIL:
        await _send_email(channel.config, monitor, incident, event, project_name)


async def send_test_alert(channel: AlertChannel, project_name: str):
    """Send a test notification to verify channel config."""
    if channel.type == AlertType.DISCORD_WEBHOOK:
        await _send_discord_test(channel.config, project_name)
    elif channel.type == AlertType.TELEGRAM:
        await _send_telegram_test(channel.config, project_name)
    elif channel.type == AlertType.EMAIL:
        await _send_email_test(channel.config, project_name)


# --- Discord ---


async def _send_discord(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    webhook_url = config["webhook_url"]

    if event == "down":
        color = 0xED4245  # red
        title = f"🔴 {monitor.name} is DOWN"
        description = f"Monitor `{monitor.url}` is not responding."
        timestamp = incident.started_at.isoformat()
    else:
        color = 0x57F287  # green
        title = f"🟢 {monitor.name} is back UP"
        duration = ""
        if incident.resolved_at and incident.started_at:
            secs = int((incident.resolved_at - incident.started_at).total_seconds())
            duration = f"\nDowntime: {_format_duration(secs)}"
        description = f"Monitor `{monitor.url}` has recovered.{duration}"
        timestamp = incident.resolved_at.isoformat() if incident.resolved_at else datetime.now(timezone.utc).isoformat()

    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "footer": {"text": f"{project_name} • UptimeBot"},
            "timestamp": timestamp,
        }]
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()


async def _send_discord_test(config: dict, project_name: str):
    webhook_url = config["webhook_url"]
    payload = {
        "embeds": [{
            "title": "✅ UptimeBot Test Alert",
            "description": "This is a test notification. Your Discord webhook is working!",
            "color": 0x5865F2,
            "footer": {"text": f"{project_name} • UptimeBot"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()


# --- Telegram ---


async def _send_telegram(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    bot_token = config["bot_token"]
    chat_id = config["chat_id"]

    if event == "down":
        text = (
            f"🔴 <b>{monitor.name} is DOWN</b>\n"
            f"URL: <code>{monitor.url}</code>\n"
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
            f"URL: <code>{monitor.url}</code>\n"
            f"Project: {project_name}{duration}"
        )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        resp.raise_for_status()


async def _send_telegram_test(config: dict, project_name: str):
    bot_token = config["bot_token"]
    chat_id = config["chat_id"]
    text = f"✅ <b>UptimeBot Test Alert</b>\nYour Telegram notifications are working!\nProject: {project_name}"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        resp.raise_for_status()


# --- Email ---


async def _send_email(config: dict, monitor: Monitor, incident: Incident, event: str, project_name: str):
    """Send email alert via SMTP. Config: {smtp_host, smtp_port, smtp_user, smtp_pass, from_email, to_email}"""
    import smtplib
    from email.mime.text import MIMEText

    if event == "down":
        subject = f"🔴 {monitor.name} is DOWN — {project_name}"
        body = f"Monitor {monitor.name} ({monitor.url}) is not responding.\nDetected at: {incident.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    else:
        duration = ""
        if incident.resolved_at and incident.started_at:
            secs = int((incident.resolved_at - incident.started_at).total_seconds())
            duration = f"\nDowntime: {_format_duration(secs)}"
        subject = f"🟢 {monitor.name} is back UP — {project_name}"
        body = f"Monitor {monitor.name} ({monitor.url}) has recovered.{duration}"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["from_email"]
    msg["To"] = config["to_email"]

    with smtplib.SMTP(config["smtp_host"], int(config.get("smtp_port", 587))) as server:
        server.starttls()
        server.login(config["smtp_user"], config["smtp_pass"])
        server.send_message(msg)


async def _send_email_test(config: dict, project_name: str):
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(f"This is a test notification from UptimeBot.\nProject: {project_name}")
    msg["Subject"] = f"✅ UptimeBot Test Alert — {project_name}"
    msg["From"] = config["from_email"]
    msg["To"] = config["to_email"]

    with smtplib.SMTP(config["smtp_host"], int(config.get("smtp_port", 587))) as server:
        server.starttls()
        server.login(config["smtp_user"], config["smtp_pass"])
        server.send_message(msg)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
