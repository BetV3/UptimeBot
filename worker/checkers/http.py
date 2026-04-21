import time

import httpx

from .base import CheckResult


# Errors that are worth a single retry — transient network flakes. Status-code
# mismatches are NOT here because they represent real signal; retrying would
# just mask legitimate outages.
TRANSIENT_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)

RETRY_BACKOFF_SECONDS = 0.5


class HttpChecker:
    def run(self, job: dict, client: httpx.Client) -> CheckResult:
        url = job["url"]
        method = job.get("method", "GET")
        timeout = job.get("timeout_seconds", 10)
        headers = job.get("headers") or {}
        body = job.get("body")
        expected = job.get("expected_status", 200)

        try:
            start = time.monotonic()
            resp = self._request_with_retry(
                client,
                method=method,
                url=url,
                headers=headers,
                content=body,
                timeout=timeout,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except httpx.TimeoutException:
            return CheckResult(status="down", error=f"Timeout after {timeout}s")
        except Exception as e:
            return CheckResult(status="down", error=str(e)[:500])

        is_up = resp.status_code == expected
        return CheckResult(
            status="up" if is_up else "down",
            response_time_ms=elapsed_ms,
            status_code=resp.status_code,
            error=None if is_up else f"Expected {expected}, got {resp.status_code}",
        )

    @staticmethod
    def _request_with_retry(client: httpx.Client, **kwargs) -> httpx.Response:
        try:
            return client.request(follow_redirects=True, **kwargs)
        except TRANSIENT_ERRORS:
            time.sleep(RETRY_BACKOFF_SECONDS)
            return client.request(follow_redirects=True, **kwargs)
