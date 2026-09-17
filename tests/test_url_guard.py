"""SSRF guard tests.

These encode the actual attack that was live in production on 2026-09-17: a
free-tier user saved an alert webhook pointing at the cloud metadata service,
pressed "Test", and the server fetched it and reported success.
"""

import pytest

from app.services.url_guard import UnsafeUrlError, validate_outbound_url


class TestBlocksInternalTargets:
    """The findings from the QA pass, as regression tests."""

    def test_blocks_cloud_metadata(self):
        # The exact URL that returned "Test alert sent." in production.
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://169.254.169.254/latest/meta-data/")

    def test_blocks_loopback(self):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://127.0.0.1:5432/")

    def test_blocks_localhost_by_name(self):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://localhost/hook")

    @pytest.mark.parametrize("url", [
        "https://10.0.0.5/hook",
        "https://192.168.1.1/hook",
        "https://172.16.4.4/hook",
        "https://[::1]/hook",
        "https://0.0.0.0/hook",
    ])
    def test_blocks_private_ranges(self, url):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url(url)

    def test_blocks_hostname_resolving_to_loopback(self):
        """The check that makes this real rather than cosmetic.

        Blocking the literal string '127.0.0.1' is trivially bypassed by
        registering a domain with a loopback A record. localtest.me is a
        public domain that resolves to 127.0.0.1 precisely for this kind of
        testing, so it stands in for an attacker-controlled one.
        """
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://localtest.me/hook")


class TestBlocksMalformed:
    @pytest.mark.parametrize("url", ["not-a-url", "", "   ", "ftp://x.com/a"])
    def test_rejects_junk(self, url):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url(url)

    def test_requires_https(self):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("http://example.com/hook")

    def test_rejects_embedded_credentials(self):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://user:pw@example.com/hook")

    def test_rejects_unresolvable_host(self):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://no-such-host-qa-12345.invalid/hook")

    def test_rejects_sensitive_port(self):
        with pytest.raises(UnsafeUrlError):
            validate_outbound_url("https://example.com:6379/hook")

    def test_error_does_not_leak_internal_address(self):
        """The message is an oracle if it names what it found.

        Echoing a port the user themselves typed reveals nothing, so the
        blocked-port message is fine. What must never leak is the resolved
        address behind a hostname — that would turn the form into a DNS
        reconnaissance tool.
        """
        with pytest.raises(UnsafeUrlError) as exc:
            validate_outbound_url("https://localtest.me/hook")
        assert "127.0.0.1" not in str(exc.value)


class TestAllowsRealWebhooks:
    """A guard that blocks everything would pass every test above."""

    @pytest.mark.parametrize("url", [
        "https://hooks.slack.com/services/T000/B000/XXXX",
        "https://discord.com/api/webhooks/123/abc",
        "https://example.com/my-hook",
    ])
    def test_allows_public_https(self, url):
        assert validate_outbound_url(url) == url


class TestSendPathIsGuarded:
    """The save-time check is a courtesy; the send-time check is the control."""

    def test_guarded_post_validates_before_sending(self, monkeypatch):
        from app.services import alerts

        called = {"posted": False}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                called["posted"] = True
                raise AssertionError("should never reach the network")

        monkeypatch.setattr(alerts.httpx, "Client", _FakeClient)

        with pytest.raises(UnsafeUrlError):
            alerts._guarded_post("https://169.254.169.254/latest/meta-data/")
        assert called["posted"] is False, "validated too late — request was made"
