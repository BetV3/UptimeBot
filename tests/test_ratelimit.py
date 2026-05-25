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
