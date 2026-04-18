import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.http import external_base_url
from app.api.routes.dashboard import _redirect_with_flash
from app.main import app


@pytest.mark.asyncio
async def test_head_request_succeeds_for_get_route():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://checkpulse.dev") as client:
        resp = await client.head("/dashboard/login")
    assert resp.status_code == 200
    assert resp.text == ""


@pytest.mark.asyncio
async def test_production_secure_requests_get_transport_headers(monkeypatch):
    settings = get_settings()
    original_env = settings.app_env
    monkeypatch.setattr(settings, "app_env", "production")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://checkpulse.dev") as client:
        resp = await client.get("/dashboard/login")

    assert resp.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "microphone=()" in resp.headers["permissions-policy"]

    monkeypatch.setattr(settings, "app_env", original_env)


@pytest.mark.asyncio
async def test_production_flash_cookie_is_secure(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")

    resp = _redirect_with_flash("/dashboard", "Nope")
    assert "Secure" in resp.headers["set-cookie"]


@pytest.mark.asyncio
async def test_external_base_url_prefers_forwarded_public_host():
    async def app_for_test(scope, receive, send):
        from starlette.requests import Request
        from starlette.responses import PlainTextResponse

        request = Request(scope, receive=receive)
        response = PlainTextResponse(external_base_url(request))
        await response(scope, receive, send)

    transport = ASGITransport(app=app_for_test)
    async with AsyncClient(
        transport=transport,
        base_url="http://api",
        headers={
            "host": "api",
            "x-forwarded-host": "checkpulse.dev",
            "x-forwarded-proto": "https",
        },
    ) as client:
        resp = await client.get("/")

    assert resp.text == "https://checkpulse.dev"
