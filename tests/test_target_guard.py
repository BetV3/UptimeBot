"""SSRF guard for monitor targets (2026-10-04).

Three layers, each tested on its own:
  1. app/services/target_guard.py  save + dispatch time (API)
  2. worker/checkers/guard.py      connect time (regional workers)
  3. HttpChecker redirect handling every hop vetted and pinned

The worker tests use REAL sockets on 127.0.0.1: a "metadata" server that
must never receive a request, and a "public" server reached through the
pinning path. Name resolution is faked so a public-looking name can map to
either address, which is exactly the DNS-rebinding / redirect attack.
"""
import http.server
import socket
import threading

import pytest

from app.services import target_guard as TG
from worker.checkers import guard as WG
from worker.checkers.dns import DnsChecker
from worker.checkers.http import HttpChecker
from worker.checkers.ssl import SslChecker

INTERNAL = ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "100.64.0.1",
            "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254"]


def _client():
    """A plain httpx client. The old checker used it (and followed redirects with
    it); the new one ignores it and builds its own pinned client per check."""
    import httpx
    return httpx.Client(trust_env=False)


def fake_dns(monkeypatch, table):
    """Make every getaddrinfo call answer from table {name: [addr, ...]}.

    IP literals resolve to themselves, as the real getaddrinfo does; that is
    what httpcore does with the pinned URL.
    """
    import ipaddress

    def gai(host, port, *a, **k):
        host = host.rstrip(".").lower()
        try:
            ipaddress.ip_address(host.strip("[]"))
            addrs = [host.strip("[]")]
        except ValueError:
            if host not in table:
                raise socket.gaierror(f"{host} not found")
            addrs = table[host]
        out = []
        for addr in addrs:
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (addr, port or 0)))
        return out
    monkeypatch.setattr(socket, "getaddrinfo", gai)


# ── 1. API-side target_guard ────────────────────────────────────────────────

@pytest.mark.parametrize("addr", INTERNAL)
def test_api_refuses_internal_literals(addr):
    host = f"[{addr}]" if ":" in addr else addr
    with pytest.raises(TG.BlockedTarget):
        TG.check_url(f"http://{host}/latest/meta-data/")
    with pytest.raises(TG.BlockedTarget):
        TG.check_host(addr, 443)


@pytest.mark.parametrize("name", ["localhost", "LOCALHOST.", "db.internal", "printer.local",
                                  "nas.home.arpa", "x.localhost", "metadata.google.internal"])
def test_api_refuses_internal_names(name):
    with pytest.raises(TG.BlockedTarget):
        TG.check_host(name)


def test_api_refuses_name_resolving_internal(monkeypatch):
    fake_dns(monkeypatch, {"evil.example": ["93.184.215.14", "169.254.169.254"]})
    with pytest.raises(TG.BlockedTarget) as e:
        TG.check_url("https://evil.example/")
    assert "169.254" not in str(e.value)  # never confirm the address


def test_api_allows_public_and_unresolved(monkeypatch):
    fake_dns(monkeypatch, {"ok.example": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]})
    TG.check_url("https://ok.example/health")
    TG.check_url("http://ok.example:8080/")
    TG.check_host("not-live-yet.example", 443)  # does not resolve yet: allowed, worker re-checks
    TG.check_url("https://93.184.215.14/")


@pytest.mark.parametrize("url", ["https://ok.example:22/", "https://ok.example:6379/", "http://ok.example:5432/"])
def test_api_refuses_scanner_ports(monkeypatch, url):
    fake_dns(monkeypatch, {"ok.example": ["93.184.215.14"]})
    with pytest.raises(TG.BlockedTarget):
        TG.check_url(url)


def test_api_refuses_credentials_and_bad_scheme():
    for u in ["https://user:pw@ok.example/", "ftp://ok.example/", "file:///etc/passwd", "gopher://x.example/"]:
        with pytest.raises(TG.BlockedTarget):
            TG.check_url(u)


@pytest.mark.parametrize("r,ok", [("", True), ("1.1.1.1", True), ("2606:4700:4700::1111", True),
                                  ("10.0.0.2", False), ("127.0.0.53", False), ("169.254.169.253", False),
                                  ("dns.internal", False)])
def test_api_resolver_rule(r, ok):
    if ok:
        TG.check_resolver(r)
    else:
        with pytest.raises(TG.BlockedTarget):
            TG.check_resolver(r)


def test_dispatch_reason_per_type(monkeypatch):
    fake_dns(monkeypatch, {"ok.example": ["93.184.215.14"], "rebound.example": ["10.1.2.3"]})
    assert TG.job_block_reason({"type": "http", "url": "https://ok.example/"}) is None
    assert TG.job_block_reason({"type": "http", "url": "https://rebound.example/"}).startswith("Blocked")
    assert TG.job_block_reason({"type": "ssl", "target_host": "rebound.example", "target_port": 443})
    assert TG.job_block_reason({"type": "dns", "target_host": "rebound.example"}) is None  # lookup only
    assert TG.job_block_reason({"type": "dns", "target_host": "ok.example", "dns_resolver": "10.0.0.2"})


# ── 2/3. worker connect-time guard, with real sockets ───────────────────────

class _Srv(http.server.BaseHTTPRequestHandler):
    hits: list = []
    routes: dict = {}

    def do_GET(self):
        type(self).hits.append((self.server.server_port, self.path, self.headers.get("Host")))
        status, headers = type(self).routes.get((self.server.server_port, self.path), (200, {}))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def servers():
    _Srv.hits, _Srv.routes = [], {}
    srvs = [http.server.HTTPServer(("127.0.0.1", 0), _Srv) for _ in range(2)]
    for s in srvs:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield [s.server_port for s in srvs]
    for s in srvs:
        s.shutdown()


@pytest.fixture
def loopback_is_public(monkeypatch):
    """Let the test reach its own 127.0.0.1 servers as if they were public,
    except an address we deliberately mark internal (192.0.2.66 stands in for
    169.254.169.254 so nothing real is ever contacted)."""
    real = WG.is_internal
    monkeypatch.setattr(WG, "is_internal",
                        lambda ip: False if str(ip) == "127.0.0.1" else real(ip))


def test_worker_blocks_internal_and_never_connects(monkeypatch, servers):
    fake_dns(monkeypatch, {"meta.example": ["169.254.169.254"]})
    calls = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError))
    r = HttpChecker().run({"url": "http://meta.example/latest/meta-data/", "timeout_seconds": 3}, _client())
    assert r.status == "down" and r.error.startswith("Blocked") and "169.254" not in r.error
    r = SslChecker().run({"target_host": "meta.example", "target_port": 443, "timeout_seconds": 3})
    assert r.status == "down" and r.error.startswith("Blocked")
    assert calls == []


def test_worker_pins_vetted_ip_and_keeps_host_header(monkeypatch, servers, loopback_is_public):
    pub, _ = servers
    fake_dns(monkeypatch, {"site.example": ["127.0.0.1"]})
    r = HttpChecker().run({"url": f"http://site.example:{pub}/health", "timeout_seconds": 3}, _client())
    assert r.status == "up", r.error
    assert _Srv.hits == [(pub, "/health", f"site.example:{pub}")]


def test_redirect_to_internal_is_blocked_before_connecting(monkeypatch, servers, loopback_is_public):
    pub, meta = servers
    # public page 302s to a name that resolves to the "metadata" server
    fake_dns(monkeypatch, {"site.example": ["127.0.0.1"], "meta.example": ["169.254.169.254"]})
    _Srv.routes[(pub, "/go")] = (302, {"Location": f"http://meta.example:{meta}/latest/meta-data/"})
    r = HttpChecker().run({"url": f"http://site.example:{pub}/go", "timeout_seconds": 3}, _client())
    assert r.status == "down" and r.error.startswith("Blocked"), r.error
    assert all(port == pub for port, _, _ in _Srv.hits), _Srv.hits  # metadata never hit


def test_redirect_to_internal_literal_is_blocked(monkeypatch, servers, loopback_is_public):
    pub, _ = servers
    fake_dns(monkeypatch, {"site.example": ["127.0.0.1"]})
    _Srv.routes[(pub, "/go")] = (301, {"Location": "http://169.254.169.254/latest/meta-data/"})
    r = HttpChecker().run({"url": f"http://site.example:{pub}/go", "timeout_seconds": 3}, _client())
    assert r.status == "down" and r.error.startswith("Blocked")


def test_public_redirect_chain_still_works(monkeypatch, servers, loopback_is_public):
    a, b = servers
    fake_dns(monkeypatch, {"a.example": ["127.0.0.1"], "b.example": ["127.0.0.1"]})
    _Srv.routes[(a, "/")] = (301, {"Location": f"http://b.example:{b}/final"})
    r = HttpChecker().run({"url": f"http://a.example:{a}/", "timeout_seconds": 3, "expected_status": 200}, _client())
    assert r.status == "up", r.error
    assert [h[0] for h in _Srv.hits] == [a, b] and _Srv.hits[1][2] == f"b.example:{b}"


def test_redirect_loop_is_down(monkeypatch, servers, loopback_is_public):
    a, _ = servers
    fake_dns(monkeypatch, {"a.example": ["127.0.0.1"]})
    _Srv.routes[(a, "/")] = (302, {"Location": "/"})
    r = HttpChecker().run({"url": f"http://a.example:{a}/", "timeout_seconds": 3}, _client())
    assert r.status == "down" and "redirect" in r.error.lower()


def test_worker_dns_checker_blocks_internal_resolver():
    r = DnsChecker().run({"target_host": "example.com", "dns_record_type": "A",
                          "dns_expected_value": "1.2.3.4", "dns_resolver": "10.0.0.2", "timeout_seconds": 2})
    assert r.status == "down" and r.error.startswith("Blocked")
    r = DnsChecker().run({"target_host": "localhost", "dns_record_type": "A",
                          "dns_expected_value": "127.0.0.1", "timeout_seconds": 2})
    assert r.status == "down" and r.error.startswith("Blocked")


@pytest.mark.parametrize("addr", INTERNAL)
def test_worker_is_internal_matches_api(addr):
    import ipaddress
    assert WG.is_internal(ipaddress.ip_address(addr))
