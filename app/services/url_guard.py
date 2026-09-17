"""SSRF guard for user-supplied outbound URLs.

Any URL a user can save and make the server fetch — alert webhooks today —
has to be checked, because the server sits inside a network the user does not
get to reach. Without this, a free-tier signup can point a webhook at
``http://169.254.169.254/`` and use CheckPulse as a proxy to the cloud
metadata service, or walk ``127.0.0.1`` ports and read the error text as a
port scan.

Two rules make this actually work rather than merely look like it works:

1. **Resolve before judging.** Blocking the literal string ``127.0.0.1`` is
   theatre: an attacker registers ``evil.example.com`` with an A record of
   ``127.0.0.1`` and walks straight through. We resolve the hostname and
   check every address it returns.

2. **Check again at send time.** A hostname that resolved publicly when it was
   saved can be re-pointed at a private address later (DNS rebinding). The
   check at save time is a UX courtesy; the check immediately before the
   request is the security control.

This cannot close the rebinding window entirely — the address can change
between our resolution and httpx's — but it removes the trivial attack and
bounds the hard one. Fully closing it needs pinning the resolved IP into the
connection, which httpx does not make easy.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Ports that are never a legitimate webhook target but are interesting to
# someone scanning: databases, caches, and internal admin surfaces.
BLOCKED_PORTS = {
    22, 23, 25, 111, 135, 139, 445, 1433, 1521,
    3306, 3389, 5432, 5984, 6379, 9200, 11211, 27017,
}

ALLOWED_SCHEMES = {"https"}


class UnsafeUrlError(ValueError):
    """The URL points somewhere the server must not be made to fetch."""


def _address_is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for anything not routable on the public internet.

    ``is_global`` covers most of it, but link-local is called out explicitly
    because 169.254.169.254 (cloud metadata) is the single highest-value
    target and must never depend on a library's definition of "global".
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve {host!r}.") from exc
    return sorted({str(info[4][0]) for info in infos})


def validate_outbound_url(raw: str, *, allow_http: bool = False) -> str:
    """Return the URL if it is safe to fetch, else raise UnsafeUrlError.

    ``allow_http`` exists only for a future opt-in; the default refuses plain
    HTTP so alert payloads (which can carry a shared secret in a header) are
    not sent in clear text.
    """
    url = (raw or "").strip()
    if not url:
        raise UnsafeUrlError("URL is required.")
    if len(url) > 2048:
        raise UnsafeUrlError("URL is too long.")

    parsed = urlparse(url)
    schemes = ALLOWED_SCHEMES | ({"http"} if allow_http else set())
    if parsed.scheme not in schemes:
        raise UnsafeUrlError(
            f"URL must start with https:// (got {parsed.scheme or 'no scheme'})."
        )
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname.")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URL must not contain credentials.")

    host = parsed.hostname
    port = parsed.port
    if port is not None and port in BLOCKED_PORTS:
        raise UnsafeUrlError(f"Port {port} is not allowed.")

    # A bare IP literal is checked directly; a name is resolved first.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    addresses = [str(literal)] if literal is not None else _resolve(host)
    if not addresses:
        raise UnsafeUrlError(f"Could not resolve {host!r}.")

    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise UnsafeUrlError(f"Unusable address for {host!r}.") from None
        if _address_is_private(ip):
            # Deliberately vague: naming the address would confirm internal
            # topology to whoever is probing.
            raise UnsafeUrlError(
                "That URL resolves to an internal address and cannot be used."
            )

    return url


def assert_safe_to_fetch(url: str, *, allow_http: bool = False) -> None:
    """Send-time re-check. Raises UnsafeUrlError; callers let it propagate."""
    validate_outbound_url(url, allow_http=allow_http)
