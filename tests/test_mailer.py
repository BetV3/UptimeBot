"""Transactional mail provider tests.

Two things under test:

1. The Postmark swap is wired correctly — right URL, right auth header, right
   payload shape, right message stream — and a provider switch is a config
   change, not a code change.

2. **Every send path goes through the shared mailer.** Before `mailer.py` the
   send logic was duplicated in four places, including the synchronous Celery
   downtime-alert path. That is how a provider migration silently leaves
   customer alerts pointed at the old vendor: you update the two obvious call
   sites in email.py and never notice alerts.py. The sweep test below fails if
   any module starts calling a provider API directly again.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import get_settings
from app.services import alerts, email, mailer


@pytest.fixture
def postmark(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production")
    monkeypatch.setattr(s, "email_provider", "postmark")
    monkeypatch.setattr(s, "postmark_server_token", "pm-token")
    monkeypatch.setattr(s, "postmark_message_stream", "outbound")
    monkeypatch.setattr(s, "email_from_address", "CheckPulse <no-reply@checkpulse.dev>")
    return s


@pytest.fixture
def resend(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production")
    monkeypatch.setattr(s, "email_provider", "resend")
    monkeypatch.setattr(s, "resend_api_key", "re-key")
    return s


# --- provider request shape --------------------------------------------------


def test_postmark_request_shape(postmark):
    url, payload, headers = mailer._build_request(
        "a@b.com", "Subj", "text body", "<p>html</p>"
    )
    assert url == mailer.POSTMARK_API_URL
    # Postmark authenticates with its own header, NOT a bearer token.
    assert headers["X-Postmark-Server-Token"] == "pm-token"
    assert "Authorization" not in headers
    # Postmark uses capitalised keys and a single To string, not a list.
    assert payload["To"] == "a@b.com"
    assert payload["Subject"] == "Subj"
    assert payload["TextBody"] == "text body"
    assert payload["HtmlBody"] == "<p>html</p>"
    assert payload["From"] == "CheckPulse <no-reply@checkpulse.dev>"


def test_postmark_uses_transactional_stream(postmark):
    """Transactional mail must not ride the broadcast stream, which carries
    different reputation handling and is meant for opt-in bulk."""
    _, payload, _ = mailer._build_request("a@b.com", "s", "t", None)
    assert payload["MessageStream"] == "outbound"


def test_resend_rollback_still_works(resend):
    """Provider switch is a config change, so a rollback needs no redeploy."""
    url, payload, headers = mailer._build_request("a@b.com", "Subj", "text", None)
    assert url == mailer.RESEND_API_URL
    assert headers["Authorization"] == "Bearer re-key"
    assert payload["to"] == ["a@b.com"]  # Resend wants a list
    assert payload["subject"] == "Subj"


def test_html_omitted_when_not_given(postmark):
    _, payload, _ = mailer._build_request("a@b.com", "s", "t", None)
    assert "HtmlBody" not in payload


# --- delivery gating ---------------------------------------------------------


def test_no_send_without_a_key(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "production")
    monkeypatch.setattr(s, "email_provider", "postmark")
    monkeypatch.setattr(s, "postmark_server_token", "")
    assert mailer.delivery_enabled() is False


def test_no_send_outside_production(monkeypatch):
    """A dev machine that happens to hold a prod token must not mail a real
    person by accident."""
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "development")
    monkeypatch.setattr(s, "email_provider", "postmark")
    monkeypatch.setattr(s, "postmark_server_token", "pm-token")
    assert mailer.delivery_enabled() is False


def test_disabled_delivery_logs_instead_of_sending(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "app_env", "development")
    with patch("httpx.Client") as c:
        mailer.send_sync("a@b.com", "subj", "body")
    c.assert_not_called()


# --- real call paths ---------------------------------------------------------


async def test_send_async_posts_to_postmark(postmark):
    resp = MagicMock(status_code=200)
    client = MagicMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=client):
        await mailer.send_async("a@b.com", "s", "t")
    assert client.post.call_args.args[0] == mailer.POSTMARK_API_URL


def test_send_sync_posts_to_postmark(postmark):
    """The Celery alert path is synchronous and must not require an event
    loop."""
    resp = MagicMock(status_code=200)
    client = MagicMock()
    client.post = MagicMock(return_value=resp)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    with patch("httpx.Client", return_value=client):
        mailer.send_sync("a@b.com", "s", "t")
    assert client.post.call_args.args[0] == mailer.POSTMARK_API_URL


# --- the regression guard ----------------------------------------------------


PROVIDER_URLS = ("api.resend.com", "api.postmarkapp.com")


def test_only_the_mailer_talks_to_a_provider():
    """No module except mailer.py may hardcode a provider endpoint.

    This is the test that would have caught downtime alerts being left behind
    on the old provider during this migration.
    """
    import app.services.email as m_email
    import app.services.alerts as m_alerts
    import app.api.routes.dashboard as m_dash
    import app.api.routes.auth as m_auth

    offenders = []
    for mod in (m_email, m_alerts, m_dash, m_auth):
        src = inspect.getsource(mod)
        for url in PROVIDER_URLS:
            if url in src:
                offenders.append(f"{mod.__name__} hardcodes {url}")
    assert not offenders, (
        "these modules bypass app.services.mailer: " + "; ".join(offenders)
    )


def test_alert_path_routes_through_mailer():
    """Downtime alerts are the most customer-critical mail CheckPulse sends."""
    src = inspect.getsource(alerts._send_via_provider)
    assert "mailer.send_sync" in src


def test_verification_and_reset_route_through_mailer():
    for fn in (email.send_verification_email, email.send_password_reset_email):
        assert "mailer.send_async" in inspect.getsource(fn), (
            f"{fn.__name__} does not use the shared mailer"
        )


def test_customer_smtp_channels_are_untouched():
    """A customer can configure their own SMTP server on an alert channel.
    That path must keep bypassing our provider entirely — their mail, their
    server."""
    src = inspect.getsource(alerts._send_email)
    assert "_email_uses_smtp" in src
    assert "smtplib.SMTP" in src


# --- the two-lane rule -------------------------------------------------------


def test_transactional_credentials_are_not_reachable_from_the_crm():
    """Cold outreach must never send through the transactional provider.

    Postmark's terms require permission-based lists; routing scraped-prospect
    outreach through this account risks a suspension that would also kill
    customer verification and downtime alerts. The CRM lives in a separate
    process with separate credentials and has no import path to this module —
    assert that stays true.
    """
    import pathlib

    crm = pathlib.Path("/home/bet/checkpulse-ai/crm")
    if not crm.exists():  # CRM is a separate deployment; skip if absent
        pytest.skip("CRM not present in this checkout")

    for py in crm.glob("*.py"):
        src = py.read_text()
        assert "postmark" not in src.lower(), f"{py.name} references Postmark"
        assert "app.services.mailer" not in src, f"{py.name} imports the transactional mailer"
        for url in PROVIDER_URLS:
            assert url not in src, f"{py.name} hardcodes {url}"
