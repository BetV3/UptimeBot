"""Transactional email delivery provider.

One place that knows how to put a message on the wire, used by both the async
request path (`app/services/email.py`) and the sync Celery alert path
(`app/services/alerts.py`). Before this module the send logic was copy-pasted
across four call sites, which is how you end up switching providers in three
of them and quietly leaving downtime alerts on the old one.

Provider selection is `email_provider` in config:

  * ``postmark`` — the transactional sender for checkpulse.dev. Better
    deliverability than the alternative and the right tool for mail a person
    actually asked for (verification, password resets, downtime alerts).
  * ``resend``   — the previous provider, kept so a rollback is a one-line
    config change rather than a redeploy of old code.

**This module is for TRANSACTIONAL mail only.**

Postmark's terms require permission-based lists and prohibit unsolicited
messages; violating that risks account termination, which would take
CheckPulse's verification and downtime alerts down with it. Cold outreach
therefore never touches this code path — it goes out through a separate
sender, on a separate domain (trycheckpulse.com), under separate credentials.
Keeping the risky lane physically apart from the critical one is the whole
point of the split.

In development, or when the selected provider has no API key, messages are
logged instead of sent, so local testing cannot burn credits or mail a real
person by accident.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
POSTMARK_API_URL = "https://api.postmarkapp.com/email"

_TIMEOUT = 10.0


def _provider() -> str:
    return (get_settings().email_provider or "postmark").strip().lower()


def _api_key() -> str:
    s = get_settings()
    return s.postmark_server_token if _provider() == "postmark" else s.resend_api_key


def delivery_enabled() -> bool:
    """True when a real send would happen (production + a configured key)."""
    s = get_settings()
    return s.app_env == "production" and bool(_api_key())


def _log_instead(to_email: str, subject: str, text: str, kind: str) -> None:
    logger.warning(
        "\n==================== %s (not sent) ====================\n"
        "Provider: %s\nTo:       %s\nSubject:  %s\n%s\n"
        "======================================================",
        kind.upper(), _provider(), to_email, subject, text,
    )


def _build_request(to_email: str, subject: str, text: str, html: str | None):
    """Return (url, json_payload, headers) for the configured provider."""
    settings = get_settings()
    sender = settings.email_from_address

    if _provider() == "postmark":
        payload = {
            "From": sender,
            "To": to_email,
            "Subject": subject,
            "TextBody": text,
            # Postmark routes by stream. Transactional mail must stay on the
            # transactional stream; the broadcast stream is for opt-in bulk
            # and carries different reputation handling.
            "MessageStream": settings.postmark_message_stream,
        }
        if html:
            payload["HtmlBody"] = html
        headers = {
            "X-Postmark-Server-Token": settings.postmark_server_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return POSTMARK_API_URL, payload, headers

    payload = {"from": sender, "to": [to_email], "subject": subject, "text": text}
    if html:
        payload["html"] = html
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    return RESEND_API_URL, payload, headers


def _raise_for_status(resp: httpx.Response, to_email: str) -> None:
    if resp.status_code < 400:
        return
    logger.error(
        "%s returned %s sending to %s: %s",
        _provider(), resp.status_code, to_email, resp.text,
    )
    resp.raise_for_status()


async def send_async(
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    kind: str = "email",
) -> None:
    """Send from the async request path."""
    if not delivery_enabled():
        _log_instead(to_email, subject, text, kind)
        return
    url, payload, headers = _build_request(to_email, subject, text, html)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, headers=headers)
    _raise_for_status(resp, to_email)


def send_sync(
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    kind: str = "email",
) -> None:
    """Send from the synchronous Celery worker path (downtime alerts)."""
    if not delivery_enabled():
        _log_instead(to_email, subject, text, kind)
        return
    url, payload, headers = _build_request(to_email, subject, text, html)
    with httpx.Client(timeout=_TIMEOUT) as client:
        resp = client.post(url, json=payload, headers=headers)
    _raise_for_status(resp, to_email)
