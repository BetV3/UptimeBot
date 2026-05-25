"""Pin the rate-limit key resolver — the brute-force throttle on
/auth/login depends on these semantics.

If trust_forwarded_for is true, we MUST use the rightmost X-Forwarded-For
entry (the IP the trusted proxy directly saw). Using leftmost would let
an attacker rotate spoofed XFF values to bypass per-IP limits entirely.
"""
from unittest.mock import MagicMock

from app.core.config import get_settings
from app.core.ratelimit import client_ip


def _fake_request(headers: dict | None = None, peer_host: str = "10.0.0.1") -> MagicMock:
    request = MagicMock()
    request.headers = headers or {}
    request.client = MagicMock(host=peer_host)
    return request


def test_trust_off_uses_tcp_peer(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", False)
    req = _fake_request({"x-forwarded-for": "1.2.3.4"}, peer_host="10.0.0.1")
    assert client_ip(req) == "10.0.0.1"


def test_trust_on_uses_rightmost_xff(monkeypatch):
    """An attacker sets `X-Forwarded-For: spoof.example` to try to escape
    rate limiting. Caddy appends the real peer IP. The rightmost entry
    is the real client; the leftmost is the spoofed value we MUST ignore."""
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({"x-forwarded-for": "spoof.example, 1.2.3.4"})
    assert client_ip(req) == "1.2.3.4"


def test_trust_on_single_xff_entry(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({"x-forwarded-for": "1.2.3.4"})
    assert client_ip(req) == "1.2.3.4"


def test_trust_on_missing_xff_falls_back(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({}, peer_host="10.0.0.1")
    assert client_ip(req) == "10.0.0.1"


def test_trust_on_empty_xff_falls_back(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({"x-forwarded-for": ""}, peer_host="10.0.0.1")
    assert client_ip(req) == "10.0.0.1"


def test_trust_on_handles_extra_whitespace(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({"x-forwarded-for": "  spoof  ,  1.2.3.4  ,  "})
    assert client_ip(req) == "1.2.3.4"


def test_trust_on_prefers_cf_connecting_ip_over_xff(monkeypatch):
    """Behind Cloudflare (Tunnel / proxied DNS / Worker), CF-Connecting-IP
    is Cloudflare's authoritative real-client header. It wins over XFF
    because XFF may contain multiple appended hops when CF is in front
    of another proxy (e.g. Caddy) and "rightmost XFF" no longer cleanly
    identifies the originating client."""
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({
        "cf-connecting-ip": "203.0.113.7",
        "x-forwarded-for": "spoof, 203.0.113.7, 198.51.100.4",
    })
    assert client_ip(req) == "203.0.113.7"


def test_trust_on_cf_connecting_ip_alone(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({"cf-connecting-ip": "203.0.113.7"})
    assert client_ip(req) == "203.0.113.7"


def test_trust_on_empty_cf_falls_through_to_xff(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", True)
    req = _fake_request({
        "cf-connecting-ip": "",
        "x-forwarded-for": "spoof, 1.2.3.4",
    })
    assert client_ip(req) == "1.2.3.4"


def test_trust_off_ignores_cf_connecting_ip(monkeypatch):
    """If we don't trust the proxy chain, an attacker hitting the app
    directly could spoof CF-Connecting-IP just like XFF. Only trust it
    when trust_forwarded_for is on."""
    monkeypatch.setattr(get_settings(), "trust_forwarded_for", False)
    req = _fake_request({"cf-connecting-ip": "203.0.113.7"}, peer_host="10.0.0.1")
    assert client_ip(req) == "10.0.0.1"
