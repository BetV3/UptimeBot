"""Pin the Turnstile gate on the three endpoints that send email.

Context: /auth/register carried `@limiter.limit("5/minute")` throughout the
April–July 2026 incident in which the signup form was driven as an email
validation relay at roughly one request per 100 minutes. These tests exist to
keep the *actual* defense — a per-submission human challenge — wired to every
mail-sending path, and to pin the fail-closed behavior that makes the gate
worth having.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import get_settings
from app.core.turnstile import turnstile_enabled, verify_turnstile


def _fake_request(peer_host: str = "203.0.113.7") -> MagicMock:
    request = MagicMock()
    request.headers = {}
    request.client = MagicMock(host=peer_host)
    return request


def _mock_siteverify(payload: dict, status_code: int = 200) -> MagicMock:
    """Build a mock httpx.AsyncClient whose POST returns `payload`."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock()
        )

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# --- Disabled (dev/test) behavior -------------------------------------------


async def test_no_secret_key_skips_verification(monkeypatch):
    """With no secret configured the gate is inert, matching how
    app/services/email.py degrades to stdout without a Resend key. Local
    development and the rest of this suite must not need a live Cloudflare."""
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "")
    assert await verify_turnstile("", _fake_request()) is True
    assert turnstile_enabled() is False


# --- Enabled behavior -------------------------------------------------------


async def test_missing_token_is_rejected(monkeypatch):
    """The exact shape of the abuse: a scripted client POSTs the form fields
    directly and never solves a challenge, so no widget response is present."""
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    assert await verify_turnstile("", _fake_request()) is False


async def test_valid_token_passes(monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    client = _mock_siteverify({"success": True})
    with patch("httpx.AsyncClient", return_value=client):
        assert await verify_turnstile("good-token", _fake_request()) is True


async def test_invalid_token_is_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    client = _mock_siteverify(
        {"success": False, "error-codes": ["invalid-input-response"]}
    )
    with patch("httpx.AsyncClient", return_value=client):
        assert await verify_turnstile("forged-token", _fake_request()) is False


async def test_replayed_token_is_rejected(monkeypatch):
    """Cloudflare marks a redeemed token `timeout-or-duplicate`. Accepting a
    replay would let one solved challenge authorize unlimited signups, which
    is precisely the relay we are closing."""
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    client = _mock_siteverify(
        {"success": False, "error-codes": ["timeout-or-duplicate"]}
    )
    with patch("httpx.AsyncClient", return_value=client):
        assert await verify_turnstile("replayed-token", _fake_request()) is False


# --- Fail-closed -------------------------------------------------------------


async def test_network_error_fails_closed(monkeypatch):
    """A siteverify transport failure must NOT fall through to allow.

    If it did, an attacker able to induce a timeout would get a free bypass,
    and the gate would be worthless exactly when under pressure. Blocking
    signups during a Cloudflare outage is the better failure.
    """
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=client):
        assert await verify_turnstile("any-token", _fake_request()) is False


async def test_http_error_fails_closed(monkeypatch):
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    client = _mock_siteverify({}, status_code=500)
    with patch("httpx.AsyncClient", return_value=client):
        assert await verify_turnstile("any-token", _fake_request()) is False


async def test_non_json_body_fails_closed(monkeypatch):
    """An edge error page instead of JSON must not be read as success."""
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.side_effect = ValueError("not json")
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=client):
        assert await verify_turnstile("any-token", _fake_request()) is False


# --- Request binding ---------------------------------------------------------


async def test_remoteip_is_sent_for_token_binding(monkeypatch):
    """The solver's IP is submitted so Cloudflare can reject a token farmed
    on one host and replayed from another. It must come from the shared
    client_ip resolver, not the tunnel's own address."""
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", False)
    client = _mock_siteverify({"success": True})
    with patch("httpx.AsyncClient", return_value=client):
        await verify_turnstile("good-token", _fake_request(peer_host="198.51.100.9"))

    sent = client.post.call_args.kwargs["data"]
    assert sent["remoteip"] == "198.51.100.9"
    assert sent["secret"] == "secret"
    assert sent["response"] == "good-token"


async def test_remoteip_honors_cf_connecting_ip(monkeypatch):
    """Behind CF Tunnel the real client is in CF-Connecting-IP; binding to
    the tunnel IP instead would make every token look like it came from one
    host."""
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    request = _fake_request(peer_host="10.0.0.1")
    request.headers = {"cf-connecting-ip": "198.51.100.42"}
    client = _mock_siteverify({"success": True})
    with patch("httpx.AsyncClient", return_value=client):
        await verify_turnstile("good-token", request)

    assert client.post.call_args.kwargs["data"]["remoteip"] == "198.51.100.42"


# --- Schema wiring -----------------------------------------------------------


def test_register_schema_accepts_widget_field_name():
    """The JSON API must accept the same field name the widget injects into
    the form body, otherwise gating the HTML form just pushes the abusive
    client to POST JSON instead."""
    from app.schemas.auth import UserRegister

    body = UserRegister.model_validate(
        {
            "email": "a@example.com",
            "password": "hunter22",
            "cf-turnstile-response": "tok",
        }
    )
    assert body.turnstile_token == "tok"


def test_register_schema_defaults_to_empty_token():
    """A payload with no challenge field parses, then fails the gate at the
    route — rather than 422-ing with a message that teaches a prober the
    field name."""
    from app.schemas.auth import UserRegister

    body = UserRegister.model_validate(
        {"email": "a@example.com", "password": "hunter22"}
    )
    assert body.turnstile_token == ""
