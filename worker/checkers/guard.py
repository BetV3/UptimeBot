"""Connect-time SSRF guard for the regional workers. Stdlib only.

The central API refuses internal monitor targets when a monitor is saved and
again when a job is handed out (app/services/target_guard.py). This is the
last line: the worker resolves the target itself, refuses unless EVERY address
is public, and then connects to the address it vetted (no second lookup), so
a name re-pointed at 169.254.169.254 between the API's check and ours, or a
redirect to an internal URL, never gets a connection. Added 2026-10-04.

Error text never names the internal address, so a refused check does not
confirm anything about the worker's network.
"""
from __future__ import annotations

import ipaddress
import socket

BLOCKED_PORTS = {22, 23, 25, 111, 135, 139, 445, 1433, 1521,
                 3306, 3389, 5432, 5984, 6379, 9200, 11211, 27017}
INTERNAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet",
                     ".localdomain", ".corp", ".private")
BLOCKED_MSG = "Blocked: that address is internal and can't be monitored."


class Blocked(Exception):
    """Target is internal; record a DOWN check with this message, never connect."""


def is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified or not ip.is_global)


def _literal(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def vet(host: str, port: int | None = None) -> str:
    """Resolve host and return ONE vetted public address to connect to.

    Raises Blocked if the name is internal-only, any address it resolves to is
    internal, or the port is a scanner favourite. Raises socket.gaierror if it
    does not resolve (a normal DOWN, the checker's existing DNS error path).
    """
    h = (host or "").strip().rstrip(".").lower()
    if port is not None and port in BLOCKED_PORTS:
        raise Blocked(f"Blocked: port {port} can't be monitored.")
    lit = _literal(h)
    if lit is not None:
        if is_internal(lit):
            raise Blocked(BLOCKED_MSG)
        return str(lit)
    if h in INTERNAL_NAMES or h.endswith(INTERNAL_SUFFIXES):
        raise Blocked(BLOCKED_MSG)
    infos = socket.getaddrinfo(h, port or 443, proto=socket.IPPROTO_TCP)
    addrs = []
    for info in infos:
        a = str(info[4][0])
        ip = _literal(a)
        if ip is None or is_internal(ip):
            raise Blocked(BLOCKED_MSG)
        if a not in addrs:
            addrs.append(a)
    if not addrs:
        raise socket.gaierror(f"{h} did not resolve")
    v4 = [a for a in addrs if ":" not in a]
    return (v4 or addrs)[0]


def vet_resolver(resolver: str | None) -> None:
    r = (resolver or "").strip()
    if not r:
        return
    ip = _literal(r)
    if ip is None or is_internal(ip):
        raise Blocked("Blocked: that resolver is internal and can't be used.")


def vet_name(host: str) -> None:
    """For DNS monitors: the name is only looked up, never connected to."""
    h = (host or "").strip().rstrip(".").lower()
    lit = _literal(h)
    if lit is not None and is_internal(lit):
        raise Blocked(BLOCKED_MSG)
    if h in INTERNAL_NAMES or h.endswith(INTERNAL_SUFFIXES):
        raise Blocked(BLOCKED_MSG)
