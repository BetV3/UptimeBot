"""Signup-funnel counters: right fields, nothing personal, never breaks signup.

Unit tests run anywhere (fake Redis). The end-to-end ones drive the real
signup routes and need real Postgres + Redis (scripts/test_agent_api.sh).
"""
import asyncio
import os
import time
import uuid
from datetime import date, datetime, timezone

import pytest

from app.core import funnel


class FakeRedis:
    """Just enough of redis.asyncio for the counter."""

    def __init__(self):
        self.h: dict[str, dict[str, int]] = {}
        self.ttl: dict[str, int] = {}

    async def hincrby(self, key, field, n):
        self.h.setdefault(key, {})
        self.h[key][field] = self.h[key].get(field, 0) + n

    async def expire(self, key, seconds):
        self.ttl[key] = seconds

    async def hexists(self, key, field):
        return field in self.h.get(key, {})

    async def hkeys(self, key):
        return list(self.h.get(key, {}))


@pytest.fixture
def fake(monkeypatch):
    r = FakeRedis()
    monkeypatch.setattr(funnel, "_redis", lambda: r)
    return r


def _run(coro):
    return asyncio.run(coro)


# ── unit ────────────────────────────────────────────────────────────────────

def test_records_step_and_source_in_daily_hash(fake):
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
    _run(funnel.record("register_submit", now=now))
    _run(funnel.record("account_created", source="site-check", now=now))
    _run(funnel.record("account_created", source="site-check", now=now))
    assert fake.h == {"cp:funnel:2026-10-22": {
        "register_submit": 1, "account_created": 2, "account_created|site-check": 2}}
    assert fake.ttl["cp:funnel:2026-10-22"] == funnel.KEY_TTL_SECONDS


def test_unknown_step_is_ignored(fake):
    _run(funnel.record("someone@example.com"))
    _run(funnel.record("page_view"))
    assert fake.h == {}


@pytest.mark.parametrize("raw,clean", [
    ("site-check", "site-check"),
    ("Status-Page", "status-page"),
    ("ssl-checker", "ssl-checker"),
    ("", "direct"),
    (None, "direct"),
    ("person@example.com", "other"),
    ("example.com", "other"),
    ("<script>", "other"),
    ("x" * 40, "other"),
])
def test_clean_source(raw, clean):
    assert funnel.clean_source(raw) == clean


@pytest.mark.parametrize("ref,referer,source", [
    ("site-check", None, "site-check"),
    (None, None, "direct"),
    (None, "https://checkpulse.dev/tools/site-check/r/AbCdEfGhIjKl", "site-check"),
    (None, "https://checkpulse.dev/tools/ssl-checker?host=client.com", "ssl-checker"),
    (None, "https://checkpulse.dev/status/acme-dental", "status-page"),
    (None, "https://checkpulse.dev/?ref=status-page", "status-page"),
    (None, "https://checkpulse.dev/vs/uptimerobot", "vs-uptimerobot"),
    (None, "https://checkpulse.dev/", "direct"),
    (None, "https://news.ycombinator.com/item?id=1", "external"),
    (None, "https://checkpulse.dev/?ref=person@example.com", "other"),
])
def test_signup_source(ref, referer, source):
    assert funnel.signup_source(ref, referer) == source


def test_source_fields_are_bounded(fake, monkeypatch):
    monkeypatch.setattr(funnel, "MAX_SOURCES_PER_DAY", 3)
    for i in range(10):
        _run(funnel.record("account_created", source=f"spam{i}"))
    day = next(iter(fake.h.values()))
    assert len([f for f in day if "|" in f]) <= 4
    assert day["account_created|other"] == 7
    assert day["account_created"] == 10


def test_redis_down_never_raises(monkeypatch):
    def boom():
        raise ConnectionError("redis down")
    monkeypatch.setattr(funnel, "_redis", boom)
    _run(funnel.record("register_submit"))  # no exception


def test_slow_redis_is_cut_off(monkeypatch):
    class Slow(FakeRedis):
        async def hincrby(self, key, field, n):
            await asyncio.sleep(5)
    monkeypatch.setattr(funnel, "_redis", lambda: Slow())
    start = time.monotonic()
    _run(funnel.record("register_submit"))
    assert time.monotonic() - start < 2


def test_funnel_script_renders_counts_and_sources():
    from scripts import funnel as script

    class SyncFake:
        def hgetall(self, key):
            if key == "cp:funnel:2026-10-22":
                return {"register_submit": "4", "account_created": "2", "email_verified": "1",
                        "account_created|site-check": "1", "account_created|direct": "1"}
            return {}
    rows, totals, sources = script.collect(SyncFake(), 2, today=date(2026, 10, 22))
    assert [d for d, _ in rows] == ["2026-10-21", "2026-10-22"]
    assert totals["register_submit"] == 4 and totals["account_created"] == 2
    assert sources == {"site-check": 1, "direct": 1}
    text = script.render(2, rows, totals, sources)
    assert "submit -> created    50%  (2/4)" in text
    assert "site-check" in text and "\u2014" not in text and "\u2013" not in text


# ── end to end on real Postgres + Redis ─────────────────────────────────────

needs_db = pytest.mark.skipif(not os.environ.get("CP_AGENT_API_TEST"),
                              reason="needs scripts/test_agent_api.sh (real Postgres + Redis)")

BROWSER = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1", "accept": "text/html"}


@pytest.fixture
def real():
    """Real Redis, flushed; funnel client rebuilt inside the test client's loop."""
    import redis
    from app.core.config import get_settings
    r = redis.Redis.from_url(get_settings().redis_url, decode_responses=True)
    r.flushdb()
    funnel._client = None
    yield r
    funnel._client = None


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.core.database import engine
    from app.main import app
    engine.sync_engine.dispose(close=False)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def _sql(q, **kw):
    from sqlalchemy import create_engine, text
    from app.core.config import get_settings
    with create_engine(get_settings().database_url_sync).begin() as c:
        res = c.execute(text(q), kw)
        try:
            return res.fetchall()
        except Exception:
            return None


def _counts(r):
    out = {}
    for key in r.keys("cp:funnel:*"):
        for f, v in r.hgetall(key).items():
            out[f] = out.get(f, 0) + int(v)
    return out


def _register(c, email, pw="hunter2hunter2", confirm=None, ref="direct", **headers):
    return c.post("/dashboard/register", follow_redirects=False, headers={**BROWSER, **headers},
                  data={"email": email, "password": pw, "confirm_password": confirm or pw, "ref": ref})


@needs_db
def test_register_page_carries_source_tag(client, real):
    html = client.get("/dashboard/register?ref=site-check", headers=BROWSER).text
    assert 'name="ref" value="site-check"' in html
    html = client.get("/dashboard/register", headers={
        **BROWSER, "referer": "https://testserver/tools/ssl-checker?host=client.com"}).text
    assert 'name="ref" value="ssl-checker"' in html
    html = client.get("/dashboard/register?ref=person@example.com", headers=BROWSER).text
    assert 'value="other"' in html and "person@example.com" not in html


@needs_db
def test_full_signup_funnel_counts_each_step(client, real, monkeypatch):
    from app.api.routes import billing, dashboard

    email = f"funnel-{uuid.uuid4().hex[:8]}@eramirez.dev"
    # challenge failure
    async def no(*a, **k):
        return False
    monkeypatch.setattr(dashboard, "verify_turnstile", no)
    assert _register(client, email).status_code == 403
    monkeypatch.undo()
    # validation failures: mismatch, too short
    assert _register(client, email, confirm="different-pass").status_code == 400
    assert _register(client, email, pw="short").status_code == 400
    # success, attributed to the site check
    r = _register(client, email, ref="site-check")
    assert r.status_code == 303
    # duplicate address
    assert _register(client, email).status_code == 409

    uid = _sql("select id from users where email=:e", e=email)[0][0]
    # verification link (production path): give the account a token, open it
    _sql("update users set email_verified=false, verification_token='tok-" + uid.hex + "', "
         "verification_token_expires_at=now() + interval '1 hour' where id=:i", i=uid)
    assert client.get(f"/dashboard/verify?token=tok-{uid.hex}", follow_redirects=False).status_code == 303

    # first project, then a second one (not counted again)
    from app.services.auth import create_access_token
    client.cookies.set("access_token", create_access_token(str(uid)))
    _sql("update users set plan='PRO' where id=:i", i=uid)
    assert client.post("/dashboard/projects", data={"name": "Client A"}, follow_redirects=False).status_code == 303
    assert client.post("/dashboard/projects", data={"name": "Client B"}, follow_redirects=False).status_code == 303
    pid = _sql("select id from projects where user_id=:u and name='Client A'", u=uid)[0][0]

    # first monitor, then a second (not counted again)
    from app.services import target_guard
    monkeypatch.setattr(target_guard, "resolve", lambda h: ["93.184.215.14"])
    for name in ("one", "two"):
        r = client.post(f"/dashboard/projects/{pid}/monitors", follow_redirects=False, data={
            "name": name, "type": "http", "url": f"https://{name}.example.com", "method": "GET",
            "expected_status": "200", "interval_seconds": "300", "timeout_seconds": "10"})
        assert r.status_code == 303, r.headers

    # checkout started (Stripe stubbed: nothing leaves the box)
    class Obj:
        id = "cus_test"
        url = "https://checkout.stripe.test/session"
    monkeypatch.setattr(billing.settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(billing.settings, "stripe_pro_price_id", "price_pro")
    monkeypatch.setattr(billing.stripe.Customer, "create", lambda **k: Obj())
    monkeypatch.setattr(billing.stripe.checkout.Session, "create", lambda **k: Obj())
    r = client.post("/billing/checkout?plan=pro", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == Obj.url

    assert _counts(real) == {
        "register_submit": 5, "turnstile_failed": 1, "validation_failed": 3,
        "account_created": 1, "account_created|site-check": 1,
        "email_verified": 1, "first_project_created": 1, "first_monitor_created": 1,
        "checkout_started": 1,
    }
    _sql("delete from users where id=:i", i=uid)


@needs_db
def test_bulk_add_counts_first_monitor_once(client, real, monkeypatch):
    from app.services import target_guard
    from app.services.auth import create_access_token
    monkeypatch.setattr(target_guard, "resolve", lambda h: ["93.184.215.14"])
    uid, pid = uuid.uuid4(), uuid.uuid4()
    _sql("insert into users (id,email,password_hash,plan,email_verified,created_at) "
         "values (:i,:e,'x','PRO',true,now())", i=uid, e=f"funnel-{uid.hex[:8]}@eramirez.dev")
    _sql("insert into projects (id,user_id,name,slug,created_at) values (:i,:u,'p',:s,now())",
         i=pid, u=uid, s=f"p-{uid.hex[:12]}")
    client.cookies.set("access_token", create_access_token(str(uid)))
    for sites in ("a1.com\na2.com", "a3.com"):
        client.post(f"/dashboard/projects/{pid}/monitors/bulk", headers={"accept": "application/json"},
                    data={"sites": sites, "interval_seconds": "300", "timeout_seconds": "10"})
    assert _counts(real) == {"first_monitor_created": 1}
    _sql("delete from users where id=:i", i=uid)


@needs_db
def test_no_personal_data_is_stored(client, real, monkeypatch):
    """Pins the privacy promise: after a real signup, nothing in Redis names the person."""
    email = f"private-person-{uuid.uuid4().hex[:6]}@eramirez.dev"
    ip, ua = "203.0.113.77", "Mozilla/5.0 (UniqueTestAgent/9.9)"
    r = _register(client, email, ref="site-check", **{"cf-connecting-ip": ip, "user-agent": ua,
                                                       "referer": "https://mail.example.org/inbox?u=" + email})
    assert r.status_code == 303
    uid = _sql("select id from users where email=:e", e=email)[0][0]
    from app.services.auth import create_access_token
    client.cookies.set("access_token", create_access_token(str(uid)))
    client.post("/dashboard/projects", data={"name": "Secret Client Name"}, follow_redirects=False)
    pid = _sql("select id from projects where user_id=:u", u=uid)[0][0]

    keys = list(real.keys("*"))
    assert any(k.startswith("cp:funnel:") for k in keys)
    blob = repr({k: real.hgetall(k) for k in keys if real.type(k) == "hash"})
    blob += repr(keys)
    for leak in (email, email.split("@")[0], "eramirez.dev", ip, "UniqueTestAgent", "mail.example.org",
                 str(uid), uid.hex, str(pid), "Secret Client Name", "secret-client-name"):
        assert leak not in blob, leak
    # and the only fields are known steps or step|short-tag
    for k in real.keys("cp:funnel:*"):
        for field in real.hgetall(k):
            step, _, src = field.partition("|")
            assert step in funnel.STEPS
            assert src == "" or funnel.SOURCE_RE.match(src)
    _sql("delete from users where id=:i", i=uid)


@needs_db
def test_signup_works_when_redis_is_down(client, monkeypatch):
    def boom():
        raise ConnectionError("redis down")
    monkeypatch.setattr(funnel, "_redis", boom)
    email = f"funnel-down-{uuid.uuid4().hex[:6]}@eramirez.dev"
    r = _register(client, email, ref="site-check")
    assert r.status_code == 303
    assert _sql("select count(*) from users where email=:e", e=email)[0][0] == 1
    _sql("delete from users where email=:e", e=email)
