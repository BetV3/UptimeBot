import socket
import ssl as ssl_lib
import time
from datetime import datetime, timezone
from typing import Any

from .base import CheckResult


def _parse_cert_datetime(raw: str) -> datetime:
    # Example: "Jun  1 12:00:00 2026 GMT"
    return datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)


def _format_name(fields: tuple) -> str:
    # fields looks like ((('commonName', 'example.com'),), (('organizationName', 'Acme'),))
    parts = []
    for rdn in fields:
        for k, v in rdn:
            parts.append(f"{k}={v}")
    return ", ".join(parts)[:510]


class SslChecker:
    """Connect to host:port, complete TLS handshake, inspect the leaf cert."""

    def run(self, job: dict, client: Any = None) -> CheckResult:
        host = job.get("target_host")
        port = int(job.get("target_port") or 443)
        timeout = int(job.get("timeout_seconds") or 10)
        warn_days = job.get("warn_days_before_expiry")

        if not host:
            return CheckResult(status="down", error="SSL monitor missing target_host")

        try:
            start = time.monotonic()
            cert = self._fetch_peer_cert(host, port, timeout)
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except socket.timeout:
            return CheckResult(status="down", error=f"Timeout after {timeout}s")
        except ssl_lib.SSLError as e:
            return CheckResult(status="down", error=f"TLS error: {str(e)[:480]}")
        except socket.gaierror as e:
            return CheckResult(status="down", error=f"DNS error: {str(e)[:480]}")
        except OSError as e:
            return CheckResult(status="down", error=f"Connection error: {str(e)[:480]}")

        not_after = cert.get("notAfter")
        if not not_after:
            return CheckResult(status="down", error="Certificate missing notAfter")

        try:
            expires_at = _parse_cert_datetime(not_after)
        except ValueError:
            return CheckResult(status="down", error=f"Unparseable notAfter: {not_after}")

        now = datetime.now(timezone.utc)
        days_remaining = int((expires_at - now).total_seconds() // 86400)
        subject = _format_name(cert.get("subject", ()))
        issuer = _format_name(cert.get("issuer", ()))

        extra = {
            "cert_days_remaining": days_remaining,
            "cert_subject": subject or None,
            "cert_issuer": issuer or None,
        }

        if days_remaining < 0:
            return CheckResult(
                status="down",
                response_time_ms=elapsed_ms,
                error=f"Certificate expired {-days_remaining} day(s) ago",
                extra=extra,
            )

        if warn_days is not None and days_remaining <= int(warn_days):
            return CheckResult(
                status="down",
                response_time_ms=elapsed_ms,
                error=f"Certificate expires in {days_remaining} day(s)",
                extra=extra,
            )

        return CheckResult(
            status="up",
            response_time_ms=elapsed_ms,
            extra=extra,
        )

    def _fetch_peer_cert(self, host: str, port: int, timeout: int) -> dict:
        ctx = ssl_lib.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                return ssock.getpeercert()
