import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .base import CheckResult
from .guard import Blocked, vet


# Errors that are worth a single retry — transient network flakes. Status-code
# mismatches are NOT here because they represent real signal; retrying would
# just mask legitimate outages.
TRANSIENT_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
    socket.gaierror,
)

RETRY_BACKOFF_SECONDS = 0.5
MAX_REDIRECTS = 10
REDIRECT_CODES = {301, 302, 303, 307, 308}
DEFAULT_PORTS = {"http": 80, "https": 443}


def _pinned(url: str) -> tuple[str, dict, dict]:
    """Vet url's host and return (url with the vetted IP, headers, extensions).

    The connection goes to the IP we checked, never a second DNS answer. The
    Host header and TLS SNI keep the original name, so virtual hosts and
    certificate verification behave exactly as before.
    """
    parts = urlsplit(url)
    if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
        raise Blocked("Blocked: only http:// and https:// URLs can be monitored.")
    if parts.username or parts.password:
        raise Blocked("Blocked: URL must not contain a username or password.")
    host = parts.hostname
    port = parts.port
    ip = vet(host, port)
    if ip == host or ip == host.strip("[]"):  # IP literal: nothing to pin
        return url, {}, {}
    ip_netloc = f"[{ip}]" if ":" in ip else ip
    if port is not None:
        ip_netloc += f":{port}"
    pinned = urlunsplit((parts.scheme, ip_netloc, parts.path, parts.query, parts.fragment))
    host_header = host if port in (None, DEFAULT_PORTS[parts.scheme]) else f"{host}:{port}"
    ext = {"sni_hostname": host} if parts.scheme == "https" else {}
    return pinned, {"Host": host_header}, ext


class HttpChecker:
    def run(self, job: dict, client: httpx.Client | None = None) -> CheckResult:
        url = job["url"]
        method = job.get("method", "GET")
        timeout = job.get("timeout_seconds", 10)
        headers = job.get("headers") or {}
        body = job.get("body")
        expected = job.get("expected_status", 200)

        try:
            start = time.monotonic()
            resp = self._request_with_retry(
                method=method,
                url=url,
                headers=headers,
                content=body,
                timeout=timeout,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except Blocked as e:
            return CheckResult(status="down", error=str(e))
        except httpx.TimeoutException:
            return CheckResult(status="down", error=f"Timeout after {timeout}s")
        except socket.gaierror as e:
            return CheckResult(status="down", error=f"DNS error: {str(e)[:480]}")
        except Exception as e:
            return CheckResult(status="down", error=str(e)[:500])

        is_up = resp.status_code == expected
        return CheckResult(
            status="up" if is_up else "down",
            response_time_ms=elapsed_ms,
            status_code=resp.status_code,
            error=None if is_up else f"Expected {expected}, got {resp.status_code}",
        )

    @classmethod
    def _request_with_retry(cls, **kwargs) -> httpx.Response:
        try:
            return cls._request_following_redirects(**kwargs)
        except TRANSIENT_ERRORS:
            time.sleep(RETRY_BACKOFF_SECONDS)
            return cls._request_following_redirects(**kwargs)

    @staticmethod
    def _request_following_redirects(method, url, headers, content, timeout) -> httpx.Response:
        """Follow redirects by hand so EVERY hop is vetted and pinned.

        httpx's follow_redirects would re-resolve each Location itself, which
        is exactly how a public URL that 302s to http://169.254.169.254/ used
        to reach the metadata service from a worker.
        """
        # A fresh client per check: connections are keyed by the pinned IP, so a
        # shared pool could reuse a TLS session negotiated for another hostname.
        # trust_env=False: an env proxy would bypass the pinning entirely.
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            cur_url, cur_method, cur_body = url, method, content
            cur_headers = dict(headers)
            origin = urlsplit(url).hostname
            for _ in range(MAX_REDIRECTS + 1):
                pinned_url, host_hdr, ext = _pinned(cur_url)
                resp = client.request(cur_method, pinned_url, headers={**cur_headers, **host_hdr},
                                      content=cur_body, extensions=ext)
                if resp.status_code not in REDIRECT_CODES or "location" not in resp.headers:
                    return resp
                nxt = urljoin(cur_url, resp.headers["location"])
                if resp.status_code == 303 or (resp.status_code in (301, 302) and cur_method not in ("GET", "HEAD")):
                    cur_method, cur_body = "GET", None
                if urlsplit(nxt).hostname != origin:
                    # same rule as browsers/httpx: credentials do not follow a cross-origin hop
                    cur_headers = {k: v for k, v in cur_headers.items()
                                   if k.lower() not in ("authorization", "cookie", "proxy-authorization")}
                cur_url = nxt
            raise httpx.TooManyRedirects(f"Exceeded {MAX_REDIRECTS} redirects")
