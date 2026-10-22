"""Live sanity check of the worker checkers against the real internet.

    cd ~/checkpulse-ai/repo && .venv/bin/python scripts/live_checker_smoke.py

Proves the SSRF pinning did not break real checks: TLS with SNI on a pinned IP,
real redirect chains (http -> https, apex -> www), SSL expiry reads, DNS. Also
proves a real public name that 302s to the metadata address is refused.
"""
import sys

sys.path.insert(0, ".")
from worker.checkers.dns import DnsChecker  # noqa: E402
from worker.checkers.http import HttpChecker  # noqa: E402
from worker.checkers.ssl import SslChecker  # noqa: E402

CASES = [
    ("http  https://example.com/", lambda: HttpChecker().run({"url": "https://example.com/", "timeout_seconds": 10}), "up"),
    ("http  https://checkpulse.dev/health", lambda: HttpChecker().run({"url": "https://checkpulse.dev/health", "timeout_seconds": 10}), "up"),
    ("http  http://google.com (301 -> www, https)", lambda: HttpChecker().run({"url": "http://google.com/", "timeout_seconds": 10}), "up"),
    ("http  http://github.com (301 -> https)", lambda: HttpChecker().run({"url": "http://github.com/", "timeout_seconds": 10}), "up"),
    ("http  httpbin redirect-to metadata", lambda: HttpChecker().run({"url": "https://httpbin.org/redirect-to?url=http%3A%2F%2F169.254.169.254%2Flatest%2Fmeta-data%2F", "timeout_seconds": 10}), "blocked"),
    ("http  http://169.254.169.254/", lambda: HttpChecker().run({"url": "http://169.254.169.254/", "timeout_seconds": 5}), "blocked"),
    ("ssl   example.com", lambda: SslChecker().run({"target_host": "example.com", "target_port": 443, "timeout_seconds": 10, "warn_days_before_expiry": 3}), "up"),
    ("ssl   expired.badssl.com", lambda: SslChecker().run({"target_host": "expired.badssl.com", "target_port": 443, "timeout_seconds": 10}), "down"),
    ("dns   example.com A any", lambda: DnsChecker().run({"target_host": "example.com", "dns_record_type": "A", "dns_expected_value": "0.0.0.0", "dns_match_mode": "any", "timeout_seconds": 5}), "down"),
    ("dns   resolver 10.0.0.2", lambda: DnsChecker().run({"target_host": "example.com", "dns_record_type": "A", "dns_expected_value": "1.2.3.4", "dns_resolver": "10.0.0.2", "timeout_seconds": 5}), "blocked"),
]

bad = 0
for label, fn, want in CASES:
    r = fn()
    got = "blocked" if (r.error or "").startswith("Blocked") else r.status
    ok = got == want
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label:<45} want={want:<8} got={got:<8} "
          f"code={r.status_code} ms={r.response_time_ms} err={(r.error or '')[:90]}")
print(f"{len(CASES) - bad}/{len(CASES)} as expected")
sys.exit(1 if bad else 0)
