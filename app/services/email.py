"""
Email delivery (transactional).

Renders and sends verification and password-reset messages. Delivery itself
lives in `app.services.mailer`, which both this module and the synchronous
Celery alert path share, so there is exactly one place that knows how to talk
to the email provider.

In development, or when the provider has no key, links are logged to stdout so
a dev can click them straight from the server log.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.services import mailer

logger = logging.getLogger(__name__)
settings = get_settings()


def generate_verification_token() -> tuple[str, datetime]:
    """Return a fresh opaque token and its absolute expiry timestamp."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.verification_token_ttl_hours
    )
    return token, expires_at


def _resolve_base_url(base_url: str | None = None) -> str:
    return (base_url or settings.app_url).rstrip("/")


def _verification_url(token: str, base_url: str | None = None) -> str:
    base = _resolve_base_url(base_url)
    return f"{base}/dashboard/verify?token={token}"


def _render_verification_email(verify_url: str) -> tuple[str, str]:
    text = (
        "Welcome to CheckPulse!\n\n"
        "Click the link below to verify your email address and activate your account:\n\n"
        f"{verify_url}\n\n"
        f"This link will expire in {settings.verification_token_ttl_hours} hours.\n"
        "If you didn't create an account, you can safely ignore this email.\n"
    )
    html = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, system-ui, sans-serif; background:#0b0f17; color:#e6edf3; padding:32px;">
    <div style="max-width:480px; margin:0 auto; background:#111826; border:1px solid #1f2a3a; border-radius:12px; padding:32px;">
      <h1 style="margin:0 0 16px; font-size:20px;">Verify your email</h1>
      <p style="margin:0 0 24px; color:#9aa7b8;">
        Welcome to CheckPulse. Click the button below to activate your account.
      </p>
      <p style="margin:0 0 24px;">
        <a href="{verify_url}"
           style="display:inline-block; background:#7c3aed; color:#fff; text-decoration:none;
                  padding:12px 20px; border-radius:8px; font-weight:600;">
          Verify email
        </a>
      </p>
      <p style="margin:0 0 8px; font-size:12px; color:#6b7a8f;">
        Or paste this URL into your browser:
      </p>
      <p style="margin:0 0 24px; font-size:12px; color:#9aa7b8; word-break:break-all;">
        {verify_url}
      </p>
      <p style="margin:0; font-size:12px; color:#6b7a8f;">
        This link expires in {settings.verification_token_ttl_hours} hours. If you
        didn't create an account, you can ignore this email.
      </p>
    </div>
  </body>
</html>
"""
    return text, html


def generate_password_reset_token() -> tuple[str, datetime]:
    """Return a fresh opaque reset token and its absolute expiry timestamp."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return token, expires_at


def _password_reset_url(token: str, base_url: str | None = None) -> str:
    base = _resolve_base_url(base_url)
    return f"{base}/dashboard/reset-password?token={token}"


async def send_password_reset_email(to_email: str, token: str, base_url: str | None = None) -> None:
    """Send a password reset link. Dev mode logs the link to stdout."""
    reset_url = _password_reset_url(token, base_url=base_url)
    text_body = (
        "We received a request to reset your CheckPulse password.\n\n"
        f"Reset your password here:\n{reset_url}\n\n"
        "This link expires in 1 hour. If you didn't request this, ignore this email.\n"
    )
    html_body = f"""\
<!doctype html>
<html>
  <body style="font-family: -apple-system, system-ui, sans-serif; background:#0b0f17; color:#e6edf3; padding:32px;">
    <div style="max-width:480px; margin:0 auto; background:#111826; border:1px solid #1f2a3a; border-radius:12px; padding:32px;">
      <h1 style="margin:0 0 16px; font-size:20px;">Reset your password</h1>
      <p style="margin:0 0 24px; color:#9aa7b8;">
        We received a request to reset your CheckPulse password. Click the button below to choose a new one.
      </p>
      <p style="margin:0 0 24px;">
        <a href="{reset_url}"
           style="display:inline-block; background:#7c3aed; color:#fff; text-decoration:none;
                  padding:12px 20px; border-radius:8px; font-weight:600;">
          Reset password
        </a>
      </p>
      <p style="margin:0 0 8px; font-size:12px; color:#6b7a8f;">Or paste this URL into your browser:</p>
      <p style="margin:0 0 24px; font-size:12px; color:#9aa7b8; word-break:break-all;">{reset_url}</p>
      <p style="margin:0; font-size:12px; color:#6b7a8f;">
        This link expires in 1 hour. If you didn't request a reset, you can ignore this email.
      </p>
    </div>
  </body>
</html>
"""

    if not mailer.delivery_enabled():
        logger.warning(
            "\n"
            "==================== PASSWORD RESET (dev) ====================\n"
            "To:      %s\n"
            "Subject: Reset your CheckPulse password\n"
            "Link:    %s\n"
            "==============================================================",
            to_email,
            reset_url,
        )
        return

    await mailer.send_async(
        to_email,
        "Reset your CheckPulse password",
        text_body,
        html_body,
        kind="password reset",
    )


async def send_verification_email(to_email: str, token: str, base_url: str | None = None) -> None:
    """
    Send a verification email. In development the URL is logged to stdout so
    the dev can click it straight from the console; in production it's sent
    through the Resend HTTP API.
    """
    verify_url = _verification_url(token, base_url=base_url)
    text_body, html_body = _render_verification_email(verify_url)

    # Dev mode: print the link to the log regardless of whether a provider key
    # is present. This keeps local testing free and avoids accidental sends
    # from a dev machine that happens to have a prod API key in its env.
    if not mailer.delivery_enabled():
        logger.warning(
            "\n"
            "==================== EMAIL VERIFICATION (dev) ====================\n"
            "To:      %s\n"
            "Subject: Verify your CheckPulse account\n"
            "Link:    %s\n"
            "==================================================================",
            to_email,
            verify_url,
        )
        return

    await mailer.send_async(
        to_email,
        "Verify your CheckPulse account",
        text_body,
        html_body,
        kind="email verification",
    )
