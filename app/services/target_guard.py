"""SSRF guard for monitor TARGETS (what the check workers connect to).

url_guard.py protects URLs the central server fetches (webhooks, SMTP). This
module protects what a user makes the regional workers connect to: an HTTP
URL, an SSL host:port, a DNS name and an optional custom resolver. Without it
a free account can aim a monitor at 169.254.169.254 (cloud metadata) or a
worker's private network and read back status codes and error text, and
check-now makes that interactive. Found 2026-10-04.

Rules (the worker enforces the same ones at connect time, see
worker/checkers/guard.py; this is the save-time and dispatch-time copy):

- An IP literal must be public.
- A name must not be an internal-only name (localhost, *.internal, ...).
- A name that resolves must resolve ONLY to public addresses. A name that does
  not resolve yet is allowed (a monitor for a domain that is not live yet is
  legitimate); the worker re-checks every time it connects.
- Ports that are never a monitoring target but are useful to a scanner are
  refused (the url_guard list).
- A custom DNS resolver must be a public IP literal.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.services.url_guard import BLOCKED_PORTS, _address_is_private

INTERNAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet",
                     ".localdomain", ".corp", ".private")
BLOCKED_MSG = "That address is internal and can't be monitored."


class BlockedTarget(ValueError):
    """The monitor target is internal or otherwise not allowed."""


def _ip(value: str):
    try:
        return ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return None


def resolve(host: str) -> list[str]:
    """All addresses for host, or [] if it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({str(i[4][0]) for i in infos})


def check_host(host: str, port: int | None = None, *, resolve_names: bool = True) -> None:
    """Raise BlockedTarget if host (and port) must not be a monitor target."""
    h = (host or "").strip().rstrip(".").lower()
    if not h:
        raise BlockedTarget("Target host can't be blank.")
    if port is not None and port in BLOCKED_PORTS:
        raise BlockedTarget(f"Port {port} can't be monitored.")
    lit = _ip(h)
    if lit is not None:
        if _address_is_private(lit):
            raise BlockedTarget(BLOCKED_MSG)
        return
    if h in INTERNAL_NAMES or h.endswith(INTERNAL_SUFFIXES):
        raise BlockedTarget(BLOCKED_MSG)
    if not resolve_names:
        return
    for addr in resolve(h):
        ip = _ip(addr)
        if ip is None or _address_is_private(ip):
            raise BlockedTarget(BLOCKED_MSG)


def check_url(url: str) -> None:
    p = urlparse((url or "").strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BlockedTarget("URL must start with http:// or https://.")
    if p.username or p.password:
        raise BlockedTarget("URL must not contain a username or password.")
    try:
        port = p.port
    except ValueError:
        raise BlockedTarget("URL has an invalid port.") from None
    check_host(p.hostname, port)


def check_resolver(resolver: str | None) -> None:
    """A custom resolver must be a public IP literal (or empty = system default)."""
    r = (resolver or "").strip()
    if not r:
        return
    ip = _ip(r)
    if ip is None:
        raise BlockedTarget("Resolver must be an IP address, like 1.1.1.1.")
    if _address_is_private(ip):
        raise BlockedTarget("That resolver is internal and can't be used.")


def job_block_reason(job: dict) -> str | None:
    """Dispatch-time check of a worker job (same dict /internal/jobs returns).

    Returns a short reason to record as the check error, or None if the job
    may go to a worker. Used by /internal/jobs so a monitor whose name was
    re-pointed at an internal address after it was saved is refused centrally,
    even by workers that predate the worker-side guard.
    """
    try:
        t = job.get("type") or "http"
        if t == "http":
            check_url(job.get("url") or "")
        elif t == "ssl":
            check_host(job.get("target_host") or "", int(job.get("target_port") or 443))
        elif t == "dns":
            check_host(job.get("target_host") or "", resolve_names=False)
            check_resolver(job.get("dns_resolver"))
    except BlockedTarget as e:
        return f"Blocked: {e}"
    return None
