import time

import httpx

from .base import CheckResult


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
            resp = client.request(
                method=method,
                url=url,
                headers=headers,
                content=body,
                timeout=timeout,
                follow_redirects=True,
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
