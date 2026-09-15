"""Cloudflare Turnstile verification for email-sending endpoints.

Why this exists
---------------
Between April and July 2026 the signup form was used as an email-validation
relay: an automated client walked an alphabetically-ordered corporate email
list and submitted ~1 address every 100 minutes, 24/7, producing 1,192 junk
users and causing CheckPulse to send unsolicited verification mail from
`notifications.checkpulse.dev` to thousands of third-party corporate inboxes.

Rate limiting cannot stop this. `/auth/register` already carried
`@limiter.limit("5/minute")` for the whole incident; a client pacing itself at
0.01 req/min never approaches the bucket. The abuse is low-and-slow by design,
so the defense has to be a proof-of-humanity at submit time, not a frequency
cap.

Turnstile is the right fit here specifically because Cloudflare already
terminates our traffic (CF Tunnel + proxied DNS), so there is no new vendor in
the path and `CF-Connecting-IP` is already trusted by `app/core/ratelimit.py`.

Fail-closed, deliberately
-------------------------
When Turnstile is configured, a missing or invalid token is rejected. We do
NOT fall back to "allow" on a siteverify network error: the entire point is to
gate outbound email, and an attacker who can induce a timeout would otherwise
get a free bypass. A brief Cloudflare outage blocking signups is a strictly
better failure than resuming the mail relay.

When `turnstile_secret_key` is empty (dev, tests, local compose) verification
is skipped entirely, matching how `app/services/email.py` already degrades to
stdout without `resend_api_key`.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import Request

from app.core.config import get_settings

logger = logging.getLogger(__name__)

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT_SECONDS = 10.0

# Shown to the user on any rejection. Intentionally generic: distinguishing
# "missing token" from "token already redeemed" only helps someone probing the
# gate, and neither is actionable for a real person beyond retrying.
CHALLENGE_FAILED_MESSAGE = (
    "Could not verify you are human. Please refresh the page and try again."
)


def turnstile_enabled() -> bool:
    """True when a secret key is configured and the gate should be enforced."""
    return bool(get_settings().turnstile_secret_key)


def turnstile_site_key() -> str:
    """Public site key for templates. Empty string renders no widget."""
    return get_settings().turnstile_site_key


async def verify_turnstile(token: str, request: Request) -> bool:
    """Validate a Turnstile token against Cloudflare's siteverify API.

    Returns True when the challenge passed (or when Turnstile is not
    configured). Returns False on every failure mode, including transport
    errors — see the fail-closed note in the module docstring.
    """
    settings = get_settings()
    if not settings.turnstile_secret_key:
        return True

    if not token:
        # No widget response in the form body at all. Either the user
        # submitted before the challenge resolved, or the request never came
        # from our form.
        return False

    payload = {
        "secret": settings.turnstile_secret_key,
        "response": token,
    }

    # Binding the token to the solver's IP lets Cloudflare reject a token
    # farmed on one host and replayed from another. We reuse the same resolver
    # the rate limiter uses so this is correct behind CF Tunnel rather than
    # pinning every request to the tunnel's own address.
    from app.core.ratelimit import client_ip

    remote_ip = client_ip(request)
    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(SITEVERIFY_URL, data=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers a non-JSON body (e.g. an edge error page).
        logger.warning("Turnstile siteverify call failed, rejecting: %s", exc)
        return False

    if data.get("success"):
        return True

    logger.info(
        "Turnstile challenge rejected (ip=%s, codes=%s)",
        remote_ip,
        data.get("error-codes"),
    )
    return False
