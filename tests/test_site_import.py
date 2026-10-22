"""Site check -> monitors: the checked list follows the visitor into their account.

Real Postgres + Redis (scripts/test_agent_api.sh). Runs are seeded straight
into Redis in the same shape app/services/site_check.py writes, so these
tests need no workers.
"""
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("CP_AGENT_API_TEST"),
                                reason="needs scripts/test_agent_api.sh (real Postgres + Redis)")

if os.environ.get("CP_AGENT_API_TEST"):
    import redis
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from app.api.routes import tools
    from app.core.config import get_settings
    from app.main import app
    from app.services import site_import, target_guard
    from app.services.auth import create_access_token

    SET = get_settings()
    ENG = create_engine(SET.database_url_sync)
    R = redis.Redis.from_url(SET.redis_url, decode_responses=True)

BROWSER = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1", "accept": "text/html"}
SITES = ["https://alpha.com", "https://beta.com/shop", "https://gamma.org", "https://delta.net", "https://eps.io"]


def sql(q, **kw):
    with ENG.begin() as c:
        r = c.execute(text(q), kw)
        try:
            return r.fetchall()
        except Exception:
            return None


@pytest.fixture
def client():
    from app.core.database import engine
    engine.sync_engine.dispose(close=False)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    R.flushdb()
    site_import._client = None
    tools._redis_client = None
    monkeypatch.setattr(target_guard, "resolve", lambda h: [] if h.endswith(".invalid") else ["93.184.215.14"])
    yield
    site_import._client = None


def seed_run(urls=SITES, ttl=3600) -> str:
    """A finished site-check run, exactly as site_check.load() stores it."""
    run_id = uuid.uuid4().hex[:16]
    run = {"id": run_id, "created": 0, "done": True, "regions": [{"code": "us-east", "label": "New York"}],
           "sites": [{"name": u.split("//")[1], "url": u, "host": u.split("//")[1].split("/")[0],
                      "monitor_id": str(uuid.uuid4()),
                      "results": {"us-east": {"status": "up", "status_code": 200, "ms": 80, "error": None}}}
                     for u in urls]}
    R.set("cp:sitecheck:run:" + run_id, json.dumps(run), ex=ttl)
    return run_id


def make_user(plan="FREE", project=True):
    uid = uuid.uuid4()
    sql("insert into users (id,email,password_hash,plan,email_verified,created_at) "
        "values (:i,:e,'x',:p,true,now())", i=uid, e=f"sci-{uid.hex[:10]}@eramirez.dev", p=plan)
    pid = None
    if project:
        pid = uuid.uuid4()
        sql("insert into projects (id,user_id,name,slug,created_at) values (:i,:u,'Clients',:s,now())",
            i=pid, u=uid, s=f"p-{uid.hex[:12]}")
    return uid, (str(pid) if pid else None)


def login(c, uid):
    c.cookies.clear()
    c.cookies.set("access_token", create_access_token(str(uid)))


def monitor_urls(pid):
    return sorted(r[0] for r in sql("select url from monitors where project_id=:p and type='HTTP'", p=pid))


def register(c, email):
    return c.post("/dashboard/register", follow_redirects=False, headers=BROWSER,
                  data={"email": email, "password": "hunter2hunter2", "confirm_password": "hunter2hunter2",
                        "ref": "site-check"})


# ── results page ─────────────────────────────────────────────────────────────

def test_results_page_offers_to_monitor_the_checked_sites(client):
    rid = seed_run()
    html = client.get(f"/tools/site-check/r/{rid}", headers=BROWSER).text
    assert f"/dashboard/register?ref=site-check&amp;run={rid}" in html
    assert "Monitor these 5 sites free" in html
    one = seed_run(["https://solo.com"])
    assert "Monitor this site free" in client.get(f"/tools/site-check/r/{one}", headers=BROWSER).text


# ── signed-out visitor: register, verify, sign in ────────────────────────────

def test_link_stores_run_in_httponly_cookie_that_expires_with_the_run(client):
    rid = seed_run(ttl=5000)
    r = client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/register?ref=site-check"
    cookie = r.headers["set-cookie"]
    assert f"{site_import.COOKIE}={rid}" in cookie and "HttpOnly" in cookie
    assert "Path=/dashboard" in cookie
    max_age = int(cookie.lower().split("max-age=")[1].split(";")[0])
    assert 4990 <= max_age <= 5000
    # and the form itself renders, attributed to the site check
    html = client.get("/dashboard/register?ref=site-check", headers=BROWSER).text
    assert "Create Account" in html


def test_unknown_or_malformed_run_sets_nothing(client):
    for bad in ("AAAAAAAAAAAAAAAA", "x", "../../etc", "a" * 60):
        r = client.get(f"/dashboard/register?ref=site-check&run={bad}", headers=BROWSER, follow_redirects=False)
        assert r.status_code == 303
        assert site_import.COOKIE not in r.headers.get("set-cookie", "")


def test_same_browser_signup_lands_on_prefilled_bulk_add(client):
    rid = seed_run()
    client.cookies.clear()
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    email = f"sci-{uuid.uuid4().hex[:8]}@eramirez.dev"
    r = register(client, email)                       # dev mode: signed in at once
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/import/site-check"
    uid = sql("select id from users where email=:e", e=email)[0][0]
    # no project yet: the dashboard offers to create one for the sites
    r = client.get("/dashboard/import/site-check", follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    html = client.get("/dashboard", headers=BROWSER).text
    assert "5 sites are ready to monitor" in html and 'action="/dashboard/projects"' in html
    r = client.post("/dashboard/projects", data={"name": "Client sites"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert 'id="site-import"' in html and 'name="source" value="site-check"' in html
    for u in SITES:
        assert u in html
    assert "has room for 3 more monitors" in html       # Free plan, said up front
    # nothing was created until the user confirms
    assert monitor_urls(pid) == []
    sql("delete from users where id=:i", i=uid)


def test_verification_in_another_browser_still_finds_the_sites(client, monkeypatch):
    """Production path: register here, open the email link on a phone (no cookie)."""
    monkeypatch.setattr(get_settings(), "app_env", "production")
    rid = seed_run()
    client.cookies.clear()
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    email = f"sci-{uuid.uuid4().hex[:8]}@eramirez.dev"
    r = register(client, email)
    assert r.status_code == 200 and "verif" in r.text.lower()
    uid, token = sql("select id, verification_token from users where email=:e", e=email)[0]
    client.cookies.clear()                            # a different browser
    r = client.get(f"/dashboard/verify?token={token}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/import/site-check"
    # signing in later (any browser) also goes there while the list is pending
    client.cookies.clear()
    r = client.post("/dashboard/login", data={"email": email, "password": "hunter2hunter2"}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard/import/site-check"
    sql("delete from users where id=:i", i=uid)


# ── confirm: plan limits still apply, offer goes away ────────────────────────

def test_confirm_adds_up_to_plan_limit_and_reports_the_rest(client):
    uid, pid = make_user("FREE")
    rid = seed_run()
    login(client, uid)
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    r = client.post(f"/dashboard/projects/{pid}/monitors/bulk", follow_redirects=False, data={
        "sites": "\n".join(SITES), "interval_seconds": "300", "timeout_seconds": "10", "source": "site-check"})
    assert r.status_code == 303
    assert len(monitor_urls(pid)) == 3
    notice = r.headers["set-cookie"]
    assert "Skipped%202" in notice and "3-monitor%20limit" in notice
    # the offer is gone, in Redis and on the page; the browser cookie is dropped
    assert R.get(site_import.CLAIM_KEY + str(uid)) is None
    assert f'{site_import.COOKIE}=""' in notice or f"{site_import.COOKIE}=;" in notice
    client.cookies.delete(site_import.COOKIE, path="/dashboard")
    assert 'id="site-import"' not in client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    sql("delete from users where id=:i", i=uid)


def test_interval_below_plan_minimum_is_refused(client):
    uid, pid = make_user("FREE")
    rid = seed_run()
    login(client, uid)
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    r = client.post(f"/dashboard/projects/{pid}/monitors/bulk", headers={"accept": "application/json"}, data={
        "sites": "\n".join(SITES), "interval_seconds": "60", "timeout_seconds": "10", "source": "site-check"})
    assert r.status_code == 403
    assert monitor_urls(pid) == []
    assert R.get(site_import.CLAIM_KEY + str(uid)) == rid      # still offered
    sql("delete from users where id=:i", i=uid)


# ── already signed in ───────────────────────────────────────────────────────

def test_signed_in_user_goes_straight_to_prefilled_bulk_add(client):
    uid, pid = make_user("PRO")
    rid = seed_run()
    login(client, uid)
    r = client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/dashboard/import/site-check"
    r = client.get("/dashboard/import/site-check", follow_redirects=False)
    assert r.headers["location"] == f"/dashboard/projects/{pid}#site-import"
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert "Add these 5 sites" in html and "has room for" not in html   # Pro has room
    r = client.post(f"/dashboard/projects/{pid}/monitors/bulk", headers={"accept": "application/json"}, data={
        "sites": "\n".join(SITES), "interval_seconds": "60", "timeout_seconds": "10", "source": "site-check"})
    assert r.json()["created"] == 5
    assert monitor_urls(pid) == sorted(SITES)
    sql("delete from users where id=:i", i=uid)


def test_signed_in_with_expired_run_gets_a_clear_message(client):
    uid, _ = make_user("PRO")
    login(client, uid)
    r = client.get("/dashboard/register?ref=site-check&run=AAAAAAAAAAAAAAAA", headers=BROWSER, follow_redirects=False)
    assert r.headers["location"] == "/dashboard" and "expired" in r.headers["set-cookie"]
    sql("delete from users where id=:i", i=uid)


# ── who can read the list, and for how long ─────────────────────────────────

def test_list_is_only_shown_to_the_account_holding_the_run(client):
    a, pid_a = make_user("PRO")
    b, pid_b = make_user("PRO")
    rid = seed_run()
    login(client, a)
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    assert "alpha.com" in client.get(f"/dashboard/projects/{pid_a}", headers=BROWSER).text
    login(client, b)                                   # no run id: sees nothing
    html = client.get(f"/dashboard/projects/{pid_b}", headers=BROWSER).text
    assert "alpha.com" not in html and 'id="site-import"' not in html
    assert "ready to monitor" not in client.get("/dashboard", headers=BROWSER).text
    r = client.get("/dashboard/import/site-check", follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    client.cookies.clear()                             # signed out: no list anywhere
    assert client.get("/dashboard/import/site-check", follow_redirects=False).headers["location"] == "/dashboard/login"
    for u in (a, b):
        sql("delete from users where id=:i", i=u)


def test_claim_expires_with_the_run(client):
    uid, pid = make_user("PRO")
    rid = seed_run(ttl=120)
    login(client, uid)
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    claim_ttl = R.ttl(site_import.CLAIM_KEY + str(uid))
    run_ttl = R.ttl("cp:sitecheck:run:" + rid)
    assert 0 < claim_ttl <= 120 and abs(claim_ttl - run_ttl) <= 2
    R.delete("cp:sitecheck:run:" + rid)                # the run expires
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert 'id="site-import"' not in html and "alpha.com" not in html
    assert R.get(site_import.CLAIM_KEY + str(uid)) is None
    sql("delete from users where id=:i", i=uid)


def test_dismiss_forgets_the_list(client):
    uid, pid = make_user("PRO")
    rid = seed_run()
    login(client, uid)
    client.get(f"/dashboard/register?ref=site-check&run={rid}", headers=BROWSER, follow_redirects=False)
    r = client.post("/dashboard/import/site-check/dismiss", follow_redirects=False,
                    headers={"referer": f"https://testserver/dashboard/projects/{pid}"})
    assert r.headers["location"] == f"/dashboard/projects/{pid}"
    client.cookies.delete(site_import.COOKIE, path="/dashboard")
    assert R.get(site_import.CLAIM_KEY + str(uid)) is None
    assert 'id="site-import"' not in client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    # an off-site referer never becomes the redirect target
    r = client.post("/dashboard/import/site-check/dismiss", follow_redirects=False,
                    headers={"referer": "https://evil.example/phish"})
    assert r.headers["location"] == "/dashboard"
    sql("delete from users where id=:i", i=uid)


def test_redis_down_never_breaks_the_dashboard(client, monkeypatch):
    uid, pid = make_user("PRO")
    login(client, uid)

    def boom():
        raise ConnectionError("redis down")
    monkeypatch.setattr(site_import, "_redis", boom)
    assert client.get(f"/dashboard/projects/{pid}", headers=BROWSER).status_code == 200
    assert client.get("/dashboard", headers=BROWSER).status_code == 200
    sql("delete from users where id=:i", i=uid)
