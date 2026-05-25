"""Shared slowapi rate-limit key resolver.

Behind a trusted reverse proxy (TRUST_FORWARDED_FOR=true), resolve the
originating client IP in this order:

  1. `CF-Connecting-IP` — Cloudflare's first-party client header.
     Cloudflare *overwrites* any client-supplied value, so when the
     request actually passed through Cloudflare (Tunnel / proxied DNS /
     Worker) this is the authoritative real-client IP.
  2. Rightmost `X-Forwarded-For` entry — for a single non-Cloudflare
     trusted hop (e.g. Caddy or nginx directly in front of the app).
     Caddy *appends* the immediate-peer IP to XFF, so rightmost is the
     IP Caddy directly saw, and leftmost is whatever the client put in
     the request (attacker-controlled).
  3. The immediate TCP peer.

Why we don't use the leftmost XFF entry: it's client-supplied and
trivially spoofable. Using it as a rate-limit key would let an attacker
rotate fake leftmost values to bypass per-IP throttles — exactly the
brute-force vector we need to keep closed on /auth/login.

Why CF-Connecting-IP wins over XFF when both are set: with Cloudflare
in the path, XFF can contain multiple appended hops (e.g. when CF is
in front of Caddy), so "rightmost XFF" no longer cleanly identifies
the client. Cloudflare's first-party header sidesteps that.

In dev (TRUST_FORWARDED_FOR=false, no proxy), use the TCP peer
unconditionally — trusting a spoofable header without a proxy in front
would be the exact bug we're avoiding above.
"""
from fastapi import Request
from slowapi.util import get_remote_address

from app.core.config import get_settings


def client_ip(request: Request) -> str:
    if get_settings().trust_forwarded_for:
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip

        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                return parts[-1]
    return get_remote_address(request)
