import socket
import ssl as ssl_lib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from worker.checkers.ssl import SslChecker


def _cert(days_from_now: int, subject="example.com", issuer="Acme CA") -> dict:
    expiry = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return {
        "notAfter": expiry.strftime("%b %d %H:%M:%S %Y GMT"),
        "subject": ((("commonName", subject),),),
        "issuer": ((("commonName", issuer), ("organizationName", "Acme Inc")),),
    }


def _job(**overrides) -> dict:
    base = {
        "pending_check_id": "pc-1",
        "monitor_id": "m-1",
        "type": "ssl",
        "target_host": "example.com",
        "target_port": 443,
        "timeout_seconds": 10,
        "warn_days_before_expiry": 14,
        "region": "us",
    }
    base.update(overrides)
    return base


@pytest.fixture
def patched_cert():
    with patch.object(SslChecker, "_fetch_peer_cert") as mock:
        yield mock


def test_up_when_cert_valid_and_far_from_expiry(patched_cert):
    patched_cert.return_value = _cert(days_from_now=60)
    result = SslChecker().run(_job())
    assert result.status == "up"
    assert result.error is None
    assert result.extra["cert_days_remaining"] >= 59
    assert "commonName=example.com" in result.extra["cert_subject"]
    assert "commonName=Acme CA" in result.extra["cert_issuer"]


def test_down_when_cert_within_warn_window(patched_cert):
    patched_cert.return_value = _cert(days_from_now=5)
    result = SslChecker().run(_job(warn_days_before_expiry=14))
    assert result.status == "down"
    assert "expires in" in result.error
    assert result.extra["cert_days_remaining"] <= 5


def test_down_when_cert_expired(patched_cert):
    patched_cert.return_value = _cert(days_from_now=-3)
    result = SslChecker().run(_job())
    assert result.status == "down"
    assert "expired" in result.error
    assert result.extra["cert_days_remaining"] < 0


def test_warn_days_none_skips_warning(patched_cert):
    patched_cert.return_value = _cert(days_from_now=2)
    result = SslChecker().run(_job(warn_days_before_expiry=None))
    assert result.status == "up"
    assert result.extra["cert_days_remaining"] <= 2


def test_down_on_handshake_failure(patched_cert):
    patched_cert.side_effect = ssl_lib.SSLError("handshake failed")
    result = SslChecker().run(_job())
    assert result.status == "down"
    assert "TLS error" in result.error


def test_down_on_dns_failure(patched_cert):
    patched_cert.side_effect = socket.gaierror("name not known")
    result = SslChecker().run(_job())
    assert result.status == "down"
    assert "DNS error" in result.error


def test_down_on_timeout(patched_cert):
    patched_cert.side_effect = socket.timeout("slow")
    result = SslChecker().run(_job(timeout_seconds=3))
    assert result.status == "down"
    assert "Timeout" in result.error


def test_down_on_connection_refused(patched_cert):
    patched_cert.side_effect = ConnectionRefusedError("refused")
    result = SslChecker().run(_job())
    assert result.status == "down"
    assert "Connection error" in result.error


def test_missing_host_is_down():
    result = SslChecker().run(_job(target_host=None))
    assert result.status == "down"
    assert "target_host" in result.error


def test_unparseable_not_after(patched_cert):
    patched_cert.return_value = {
        "notAfter": "not a real date",
        "subject": ((("commonName", "x"),),),
        "issuer": ((("commonName", "y"),),),
    }
    result = SslChecker().run(_job())
    assert result.status == "down"
    assert "Unparseable" in result.error


def test_retry_succeeds_after_transient_failure(patched_cert):
    patched_cert.side_effect = [socket.timeout("flaky"), _cert(days_from_now=60)]
    result = SslChecker().run(_job())
    assert result.status == "up"
    assert result.extra["cert_days_remaining"] >= 59


def test_no_retry_on_ssl_error(patched_cert):
    # SSLError is real signal (cert validation failure). Should NOT retry.
    patched_cert.side_effect = ssl_lib.SSLError("handshake failed")
    result = SslChecker().run(_job())
    assert result.status == "down"
    assert "TLS error" in result.error
    # One call, no retry.
    assert patched_cert.call_count == 1
