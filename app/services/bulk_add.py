"""Turn a pasted list of client sites into monitor URLs (dashboard "Bulk add").

Agencies arrive with 20-40 client sites. One form per site is the hurdle, so the
dashboard accepts a paste: one site per line, or separated by commas or spaces.
Bare domains get https://. Every surviving URL still goes through the same
validator as the single-monitor form (scheme, shape, SSRF guard), in the route.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_SITES = 100          # per paste; the plan limit usually bites first
NAME_MAX = 100           # Monitor.name column

_SPLIT = re.compile(r"[\s,;]+")
_STRIP = "<>\"'()[]{}"
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)


@dataclass
class Site:
    raw: str
    url: str | None = None
    host: str | None = None
    name: str | None = None
    error: str | None = None


def _normalize(token: str) -> Site:
    raw = token
    t = token.strip().strip(_STRIP).rstrip(".")
    if not t:
        return Site(raw=raw, error="empty")
    if not _SCHEME.match(t):
        t = "https://" + t
    p = urlsplit(t)
    scheme = p.scheme.lower()
    if scheme not in ("http", "https"):
        return Site(raw=raw, error="only http:// and https:// sites can be added")
    host = (p.hostname or "").lower().rstrip(".")
    if not host or "." not in host:
        return Site(raw=raw, error="not a site address")
    netloc = host if p.port is None else f"{host}:{p.port}"
    if p.username or p.password:
        return Site(raw=raw, error="remove the username or password from the address")
    path = p.path if p.path not in ("", "/") else ""
    url = urlunsplit((scheme, netloc, path, p.query, ""))
    shown = host[4:] if host.startswith("www.") else host
    name = (shown + path.rstrip("/"))[:NAME_MAX]
    return Site(raw=raw, url=url, host=host, name=name)


def parse(text: str) -> list[Site]:
    """Split, normalize and de-duplicate. Order is kept; duplicates are dropped silently."""
    out: list[Site] = []
    seen: set[str] = set()
    for token in _SPLIT.split(text or ""):
        if not token.strip():
            continue
        s = _normalize(token)
        key = (s.url or s.raw).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def same_url(a: str, b: str) -> bool:
    """Treat https://a.com and https://a.com/ as the same monitor."""
    return a.rstrip("/").lower() == b.rstrip("/").lower()
