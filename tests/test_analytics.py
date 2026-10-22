"""Page-view counting: counts the right things, stores nothing personal, never breaks a page."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core import analytics
from app.main import app

BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class FakeRedis:
    def __init__(self):
        self.h: dict[str, dict[str, int]] = {}
        self.ttl: dict[str, int] = {}

    async def hincrby(self, key, field, n):
        self.h.setdefault(key, {})
        self.h[key][field] = self.h[key].get(field, 0) + n

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def hlen(self, key):
        return len(self.h.get(key, {}))

    async def hexists(self, key, field):
        return field in self.h.get(key, {})


@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(analytics, "_redis", lambda: r)
    return r


def _fields(r):
    return {f: n for day in r.h.values() for f, n in day.items()}


def _get(client, path, **headers):
    h = {"user-agent": BROWSER, "accept": HTML}
    h.update(headers)
    return client.get(path, headers=h, follow_redirects=False)


# ── unit: labels and sources ────────────────────────────────────────────────

@pytest.mark.parametrize("path,label", [
    ("/", "/"),
    ("/docs/getting-started", "/docs/getting-started"),
    ("/vs/uptimerobot", "/vs/uptimerobot"),
    ("/status/acme-dental-clinic", "/status/*"),
    ("/dashboard", None),
    ("/dashboard/projects/123", None),
    ("/api/docs", None),
    ("/static/og.png", None),
    ("/health", None),
])
def test_page_label(path, label):
    assert analytics.page_label(path) == label


def test_status_slug_never_stored():
    # The slug names an agency's own client; it must collapse to one bucket.
    assert analytics.page_label("/status/secret-client-name") == "/status/*"


@pytest.mark.parametrize("qs,ref,source", [
    ("ref=hn", None, "ref:hn"),
    ("utm_source=Reddit", None, "ref:reddit"),
    ("ref=<script>alert(1)</script>", None, "ref:scriptalert1script"),
    ("", "https://news.ycombinator.com/item?id=1", "news.ycombinator.com"),
    ("", "https://www.google.com/search?q=uptime+monitor+for+agencies", "google.com"),
    ("", "https://checkpulse.dev/docs/getting-started", "internal"),
    ("", None, "direct"),
])
def test_traffic_source(qs, ref, source):
    assert analytics.traffic_source(qs, ref) == source


def test_referrer_path_and_query_never_stored():
    src = analytics.traffic_source("", "https://example.com/private/path?email=a@b.com")
    assert src == "example.com"


@pytest.mark.parametrize("ua", [
    "Googlebot/2.1 (+http://www.google.com/bot.html)",
    "curl/8.5.0",
    "python-httpx/0.28.1",
    "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)",
    "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)",
    "facebookexternalhit/1.1",
    "UptimeRobot/2.0",
    "",
    None,
])
def test_bots_not_human(ua):
    assert analytics.is_human(ua, HTML) is False


def test_browser_without_html_accept_not_counted():
    assert analytics.is_human(BROWSER, "*/*") is False
    assert analytics.is_human(BROWSER, HTML) is True


# ── integration: through the real app ───────────────────────────────────────

def test_counts_human_landing_view_with_source(fake):
    with TestClient(app) as c:
        assert _get(c, "/?ref=hn").status_code == 200
    assert _fields(fake) == {"/|ref:hn": 1}
    assert all(k.startswith("cp:pv:") for k in fake.h)
    assert set(fake.ttl.values()) == {analytics.KEY_TTL_SECONDS}


def test_ignores_bots_head_dashboard_and_errors(fake):
    with TestClient(app) as c:
        _get(c, "/", **{"user-agent": "Googlebot/2.1"})
        c.head("/", headers={"user-agent": BROWSER, "accept": HTML})
        _get(c, "/dashboard")                     # not a public page
        _get(c, "/vs/not-a-real-page")            # counted label, but 404 is not a view
    assert _fields(fake) == {}


def test_nothing_personal_in_stored_keys(fake):
    with TestClient(app) as c:
        _get(c, "/", referer="https://mail.google.com/mail/u/0/#inbox/abc?x=person@example.com",
             **{"cf-connecting-ip": "203.0.113.7"})
    blob = repr(fake.h)
    for leak in ("203.0.113.7", "person@example.com", "Safari", "inbox", "/mail/"):
        assert leak not in blob


def test_field_cap_bounds_visitor_minted_refs(fake, monkeypatch):
    monkeypatch.setattr(analytics, "MAX_FIELDS_PER_DAY", 3)

    async def run():
        for i in range(10):
            await analytics._incr("2026-10-02", f"/|ref:spam{i}")
    asyncio.run(run())
    day = fake.h["cp:pv:2026-10-02"]
    assert len(day) <= 4  # 3 real + the shared "other" bucket
    assert day["/|other"] == 7


def test_redis_down_never_breaks_page(monkeypatch):
    def boom():
        raise ConnectionError("redis down")
    monkeypatch.setattr(analytics, "_redis", boom)
    with TestClient(app) as c:
        r = _get(c, "/")
    assert r.status_code == 200
    assert "CheckPulse" in r.text
