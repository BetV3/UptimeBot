import time
from typing import Any

import dns.exception
import dns.resolver

from .base import CheckResult


TRANSIENT_ERRORS = (dns.exception.Timeout, dns.resolver.NoNameservers)

RETRY_BACKOFF_SECONDS = 0.5


def _normalize(value: str) -> str:
    # DNS names are case-insensitive and conventionally end with a trailing dot
    # in zone format. Normalize so "Example.com" and "example.com." match
    # whatever the resolver returns.
    return value.strip().rstrip(".").lower()


def _split_expected(raw: str) -> list[str]:
    return [_normalize(v) for v in raw.split(",") if v.strip()]


class DnsChecker:
    """Resolve a name and assert every expected value appears in the answer set."""

    def run(self, job: dict, client: Any = None) -> CheckResult:
        host = job.get("target_host")
        record_type = job.get("dns_record_type")
        expected_raw = job.get("dns_expected_value") or ""
        resolver_ip = job.get("dns_resolver")
        timeout = int(job.get("timeout_seconds") or 10)

        if not host:
            return CheckResult(status="down", error="DNS monitor missing target_host")
        if not record_type:
            return CheckResult(status="down", error="DNS monitor missing dns_record_type")

        expected = _split_expected(expected_raw)
        if not expected:
            return CheckResult(status="down", error="DNS monitor missing dns_expected_value")

        try:
            start = time.monotonic()
            answers = self._resolve_with_retry(host, record_type, resolver_ip, timeout)
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except dns.resolver.NXDOMAIN:
            return CheckResult(status="down", error=f"NXDOMAIN: {host}")
        except dns.resolver.NoAnswer:
            return CheckResult(status="down", error=f"No {record_type} record for {host}")
        except dns.exception.Timeout:
            return CheckResult(status="down", error=f"DNS timeout after {timeout}s")
        except dns.resolver.NoNameservers as e:
            return CheckResult(status="down", error=f"No nameservers: {str(e)[:480]}")
        except dns.exception.DNSException as e:
            return CheckResult(status="down", error=f"DNS error: {str(e)[:480]}")

        resolved = [_normalize(str(rdata)) for rdata in answers]
        resolved_joined = ", ".join(resolved)[:2000]
        extra = {"dns_resolved_values": resolved_joined}

        missing = [v for v in expected if v not in resolved]
        if missing:
            return CheckResult(
                status="down",
                response_time_ms=elapsed_ms,
                error=f"Expected {', '.join(missing)} not in {resolved_joined or '(empty)'}",
                extra=extra,
            )

        return CheckResult(
            status="up",
            response_time_ms=elapsed_ms,
            extra=extra,
        )

    def _resolve_with_retry(self, host: str, record_type: str, resolver_ip: str | None, timeout: int):
        try:
            return self._resolve(host, record_type, resolver_ip, timeout)
        except TRANSIENT_ERRORS:
            time.sleep(RETRY_BACKOFF_SECONDS)
            return self._resolve(host, record_type, resolver_ip, timeout)

    @staticmethod
    def _resolve(host: str, record_type: str, resolver_ip: str | None, timeout: int):
        resolver = dns.resolver.Resolver()
        if resolver_ip:
            resolver.nameservers = [resolver_ip]
        resolver.timeout = timeout
        resolver.lifetime = timeout
        return resolver.resolve(host, record_type)
