"""Shared slowapi rate-limit key resolver.

Behind a reverse proxy (TRUST_FORWARDED_FOR=true), take the RIGHTMOST
X-Forwarded-For entry. Caddy (the documented prod reverse proxy)
*appends* the immediate-peer IP to whatever XFF the client sent, so:

  - rightmost  = the IP Caddy directly saw — i.e. the real client (or
                 the outermost proxy you've explicitly trusted),
  - leftmost   = whatever the client put in the request, attacker-
                 spoofable.

Using the leftmost would let an attacker bypass per-IP rate limits
(critical for the brute-force throttle on /auth/login) by rotating
spoofed XFF values.

This assumes EXACTLY ONE trusted proxy hop. If you later put Cloudflare
or another CDN in front of Caddy, switch to the CDN's first-party
client header (e.g. CF-Connecting-IP) or take the Nth-from-rightmost
entry where N = trusted-hop count.

In dev (TRUST_FORWARDED_FOR=false, no proxy), fall back to the
immediate TCP peer — trusting a spoofable header without a proxy in
front would be the exact bug we're avoiding above.
"""
from fastapi import Request
from slowapi.util import get_remote_address

from app.core.config import get_settings


def client_ip(request: Request) -> str:
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                return parts[-1]
    return get_remote_address(request)
