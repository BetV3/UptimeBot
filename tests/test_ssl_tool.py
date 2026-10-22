"""SSL checker tool: input handling, SSRF refusal, rate limits fail closed, page renders."""
import socket
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.routes import tools
from app.main import app
from app.services import ssl_probe
from app.services.ssl_probe import CertReport, ProbeError, normalize_host

H = {"user-agent": "Mozilla/5.0 Chrome/128", "accept": "text/html"}


# ── input normalisation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,host", [
    ("example.com", "example.com"),
    ("  Example.COM  ", "example.com"),
    ("https://www.example.com/path?q=1#x", "www.example.com"),
    ("example.com:443", "example.com"),
    ("example.com.", "example.com"),
    ("bücher.de", "xn--bcher-kva.de"),
])
def test_normalize_ok(raw, host):
    assert normalize_host(raw) == host


@pytest.mark.parametrize("raw", [
    "", "   ", "localhost", "10.0.0.1", "169.254.169.254", "[::1]",
    "example.com:22", "example.com:8443", "user@example.com",
    "-bad.example.com", "a" * 300 + ".com", "exa mple.com", "example",
])
def test_normalize_rejects(raw):
    with pytest.raises(ProbeError):
        normalize_host(raw)


# ── SSRF: names that resolve inward are refused before any connection ─────

@pytest.mark.parametrize("addr", ["127.0.0.1", "10.1.2.3", "192.168.0.10", "169.254.169.254", "::1", "100.64.0.1"])
def test_private_resolution_refused_without_connecting(monkeypatch, addr):
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    monkeypatch.setattr(ssl_probe.socket, "getaddrinfo",
                        lambda *a, **k: [(fam, socket.SOCK_STREAM, 6, "", (addr, 443))])

    def no_connect(*a, **k):
        raise AssertionError("must not connect to a private address")
    monkeypatch.setattr(ssl_probe.socket, "create_connection", no_connect)
    with pytest.raises(ProbeError) as e:
        ssl_probe.probe("evil.example.com")
    assert addr not in str(e.value)  # error text never confirms internal topology


def test_mixed_public_and_private_answers_refused(monkeypatch):
    monkeypatch.setattr(ssl_probe.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(ProbeError):
        ssl_probe.probe("rebind.example.com")


def test_connects_to_vetted_ip_on_443_only(monkeypatch):
    seen = {}
    monkeypatch.setattr(ssl_probe.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))])

    def fake_connect(addr, timeout):
        seen["addr"] = addr
        raise ConnectionRefusedError
    monkeypatch.setattr(ssl_probe.socket, "create_connection", fake_connect)
    with pytest.raises(ProbeError):
        ssl_probe.probe("example.com")
    assert seen["addr"] == ("93.184.215.14", 443)


# ── verdicts ────────────────────────────────────────────────────────────────

def _report(days, trusted=True):
    now = datetime.now(timezone.utc)
    return CertReport(host="example.com", trusted=trusted, trust_error=None if trusted else "self-signed certificate",
                      subject_cn="example.com", issuer="Let's Encrypt", sans=["example.com", "www.example.com"],
                      not_before=now - timedelta(days=60), not_after=now + timedelta(days=days, hours=1),
                      tls_version="TLSv1.3")


@pytest.mark.parametrize("days,trusted,verdict", [
    (60, True, "ok"), (10, True, "expiring"), (-3, True, "expired"), (60, False, "untrusted"), (-3, False, "expired"),
])
def test_verdict(days, trusted, verdict):
    assert _report(days, trusted).verdict == verdict


# ── route ───────────────────────────────────────────────────────────────────

@pytest.fixture
def no_limits(monkeypatch):
    async def ok(_visitor):
        return None
    monkeypatch.setattr(tools, "allowed", ok)


def test_empty_form_renders_and_is_indexable(no_limits):
    r = TestClient(app).get("/tools/ssl-checker", headers=H)
    assert r.status_code == 200
    assert "SSL certificate checker" in r.text
    assert 'content="noindex"' not in r.text
    assert "https://checkpulse.dev/tools/ssl-checker" in r.text  # canonical


def test_result_page(no_limits, monkeypatch):
    monkeypatch.setattr(tools, "probe", lambda h: _report(10))
    r = TestClient(app).get("/tools/ssl-checker", params={"host": "example.com"}, headers=H)
    assert r.status_code == 200
    assert "10 days left" in r.text
    assert "expires soon" in r.text
    assert "Let&#39;s Encrypt" in r.text or "Let's Encrypt" in r.text
    assert 'content="noindex"' in r.text  # result pages stay out of the index
    assert r.headers.get("cache-control") == "no-store"
    assert "/dashboard/register?ref=ssl-checker" in r.text


def test_result_escapes_hostile_cert_fields(no_limits, monkeypatch):
    rep = _report(30)
    rep.issuer = "<script>alert(1)</script>"
    rep.sans = ['"><img src=x onerror=alert(1)>']
    monkeypatch.setattr(tools, "probe", lambda h: rep)
    r = TestClient(app).get("/tools/ssl-checker", params={"host": "example.com"}, headers=H)
    assert "<script>alert(1)</script>" not in r.text
    assert "<img src=x" not in r.text


def test_probe_error_shown(no_limits, monkeypatch):
    def boom(h):
        raise ProbeError("example.com refused the connection on port 443.")
    monkeypatch.setattr(tools, "probe", boom)
    r = TestClient(app).get("/tools/ssl-checker", params={"host": "example.com"}, headers=H)
    assert r.status_code == 200
    assert "refused the connection" in r.text


def test_rate_limited_returns_429_without_probing(monkeypatch):
    async def deny(_v):
        return "Too many lookups. Wait a minute and try again."
    monkeypatch.setattr(tools, "allowed", deny)
    monkeypatch.setattr(tools, "probe", lambda h: (_ for _ in ()).throw(AssertionError("probed while limited")))
    r = TestClient(app).get("/tools/ssl-checker", params={"host": "example.com"}, headers=H)
    assert r.status_code == 429
    assert "Too many lookups" in r.text


class _Counter:
    def __init__(self):
        self.n = {}

    async def incr(self, k):
        self.n[k] = self.n.get(k, 0) + 1
        return self.n[k]

    async def expire(self, k, s):
        pass


def test_limits_enforced_per_visitor_and_globally(monkeypatch):
    import asyncio
    c = _Counter()
    monkeypatch.setattr(tools, "_redis", lambda: c)

    async def run():
        out = [await tools.allowed("1.2.3.4") for _ in range(tools.PER_MINUTE + 1)]
        return out
    res = asyncio.run(run())
    assert all(r is None for r in res[:tools.PER_MINUTE])
    assert res[-1] and "Too many" in res[-1]

    c.n["cp:tool:ssl:global"] = tools.GLOBAL_PER_HOUR
    assert "busy" in asyncio.run(tools.allowed("5.6.7.8"))


def test_limits_fail_closed_when_redis_down(monkeypatch):
    import asyncio

    def down():
        raise ConnectionError("redis down")
    monkeypatch.setattr(tools, "_redis", down)
    assert "unavailable" in asyncio.run(tools.allowed("1.2.3.4"))
