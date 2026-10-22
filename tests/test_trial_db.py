"""No-card Pro trial on real Postgres + Redis (scripts/test_agent_api.sh).

Covers: start, double start refused, unverified refused, limits during the
trial, expiry pausing exactly the right monitors, upgrade resuming them, the
Stripe webhook clearing the trial, the reminder email, and the migration
round trip.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("CP_AGENT_API_TEST"),
                                reason="needs scripts/test_agent_api.sh (real Postgres + Redis)")

if os.environ.get("CP_AGENT_API_TEST"):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from app.api.routes import billing
    from app.core.config import get_settings
    from app.main import app
    from app.services import target_guard, trial
    from app.services.auth import create_access_token
    from app.workers import tasks

    SET = get_settings()
    ENG = create_engine(SET.database_url_sync)

BROWSER = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1", "accept": "text/html"}
ROOT = Path(__file__).resolve().parents[1]


def sql(q, **kw):
    with ENG.begin() as c:
        r = c.execute(text(q), kw)
        try:
            return r.fetchall()
        except Exception:
            return None


@pytest.fixture(scope="module")
def client():
    from app.core.database import engine
    engine.sync_engine.dispose(close=False)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.setattr(target_guard, "resolve", lambda h: ["93.184.215.14"])


def make_user(plan="FREE", verified=True, trial_ends=None, used=None):
    uid = uuid.uuid4()
    sql("insert into users (id,email,password_hash,plan,email_verified,created_at,trial_ends_at,trial_used) "
        "values (:i,:e,'x',:p,:v,now(),:t,:u)", i=uid, e=f"trial-{uid.hex[:10]}@eramirez.dev", p=plan,
        v=verified, t=trial_ends, u=bool(trial_ends) if used is None else used)
    return uid


def make_project(uid, age_minutes, name=None):
    pid = uuid.uuid4()
    sql("insert into projects (id,user_id,name,slug,created_at) values (:i,:u,:n,:s,now() - make_interval(mins => :a))",
        i=pid, u=uid, n=name or f"proj-{pid.hex[:6]}", s=f"p-{pid.hex[:12]}", a=age_minutes)
    return pid


def make_monitor(pid, age_minutes, interval=60, active=True, reason=None, name=None):
    mid = uuid.uuid4()
    sql("insert into monitors (id,project_id,name,type,url,method,expected_status,interval_seconds,"
        "timeout_seconds,is_active,paused_reason,current_status,created_at,next_check_at) values "
        "(:i,:p,:n,'HTTP',:u,'GET',200,:iv,10,:a,:r,'UNKNOWN',now() - make_interval(mins => :age),now())",
        i=mid, p=pid, n=name or f"m-{mid.hex[:6]}", u=f"https://{mid.hex[:8]}.example.com", iv=interval,
        a=active, r=reason, age=age_minutes)
    return mid


def mstate(mid):
    return tuple(sql("select is_active, paused_reason, interval_seconds from monitors where id=:i", i=mid)[0])


def login(c, uid):
    c.cookies.clear()
    c.cookies.set("access_token", create_access_token(str(uid)))


def urow(uid):
    return sql("select plan, trial_ends_at, trial_used, trial_expired_at, trial_reminder_sent_at "
               "from users where id=:i", i=uid)[0]


# ── start ────────────────────────────────────────────────────────────────────

def test_billing_page_offers_trial_and_start_works(client):
    uid = make_user()
    login(client, uid)
    html = client.get("/dashboard/billing", headers=BROWSER).text
    assert "Try Pro free for 14 days, no card" in html and 'action="/dashboard/billing/trial"' in html
    r = client.post("/dashboard/billing/trial", follow_redirects=False,
                    headers={"referer": "https://testserver/dashboard/billing"})
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/billing"
    assert "Pro%20trial%20started" in r.headers["set-cookie"]
    plan, ends, used, _, _ = urow(uid)
    assert plan == "FREE" and used is True
    left = ends - datetime.now(timezone.utc)
    assert timedelta(days=13, hours=23) < left <= timedelta(days=14)
    html = client.get("/dashboard/billing", headers=BROWSER).text
    assert "Pro trial: 14 days left" in html and "PRO TRIAL" in html
    assert "Try Pro free for 14 days, no card" not in html            # no second offer


def test_double_start_refused(client):
    uid = make_user()
    login(client, uid)
    client.post("/dashboard/billing/trial", follow_redirects=False)
    first_end = urow(uid)[1]
    r = client.post("/dashboard/billing/trial", follow_redirects=False)
    assert r.status_code == 303 and "already%20used" in r.headers["set-cookie"]
    assert urow(uid)[1] == first_end                                   # not extended
    # and after it ran out, still refused
    sql("update users set trial_ends_at = now() - interval '1 day' where id=:i", i=uid)
    r = client.post("/dashboard/billing/trial", headers={"accept": "application/json"})
    assert r.status_code == 409 and "already used" in r.json()["detail"]


def test_unverified_refused(client):
    uid = make_user(verified=False)
    login(client, uid)                       # test env lets unverified users in; prod would not
    r = client.post("/dashboard/billing/trial", headers={"accept": "application/json"})
    assert r.status_code == 409 and "Verify your email" in r.json()["detail"]
    assert urow(uid)[2] is False
    assert "Verify your email address to unlock" in client.get("/dashboard/billing", headers=BROWSER).text


def test_paid_user_cannot_start(client):
    uid = make_user("STARTER")
    login(client, uid)
    r = client.post("/dashboard/billing/trial", headers={"accept": "application/json"})
    assert r.status_code == 409
    assert "Try Pro free" not in client.get("/dashboard/billing", headers=BROWSER).text


def test_concurrent_starts_give_one_trial(client):
    """Four simultaneous clicks: exactly one trial. Runs in the app's own event loop."""
    import asyncio
    import httpx
    uid = make_user()
    cookie = {"cookie": f"access_token={create_access_token(str(uid))}", "accept": "application/json"}

    async def burst():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as ac:
            rs = await asyncio.gather(*(ac.post("/dashboard/billing/trial", headers=cookie) for _ in range(4)))
        return sorted(r.status_code for r in rs)
    codes = client.portal.call(burst)
    assert codes == [303, 409, 409, 409], codes
    assert urow(uid)[2] is True


# ── limits during the trial ─────────────────────────────────────────────────

def test_limits_during_trial(client):
    uid = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=5))
    login(client, uid)
    for i in range(3):
        r = client.post("/dashboard/projects", data={"name": f"Client {i}"}, follow_redirects=False)
        assert r.status_code == 303                                     # Free allows 1
    pid = sql("select id from projects where user_id=:u order by created_at limit 1", u=uid)[0][0]
    r = client.post(f"/dashboard/projects/{pid}/monitors/bulk", headers={"accept": "application/json"}, data={
        "sites": "\n".join(f"s{i}.com" for i in range(12)), "interval_seconds": "60", "timeout_seconds": "10"})
    assert r.status_code == 200 and r.json()["created"] == 12            # 60 s and > 3 monitors
    r = client.post(f"/dashboard/projects/{pid}/monitors/bulk", headers={"accept": "application/json"}, data={
        "sites": "fast.com", "interval_seconds": "30", "timeout_seconds": "10"})
    assert r.status_code == 403 and "Pro trial" in r.json()["detail"]   # 30 s is paid Pro only
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert "Pro trial: 5 days left" in html and 'min="60"' in html
    # the cap is 100, not paid Pro's 200
    sql("insert into monitors (id,project_id,name,type,url,method,expected_status,interval_seconds,timeout_seconds,"
        "is_active,current_status,created_at) select gen_random_uuid(), :p, 'bulk-'||g, 'HTTP', 'https://b'||g||'.example.com',"
        "'GET',200,60,10,true,'UNKNOWN',now() from generate_series(1,88) g", p=pid)
    r = client.post(f"/dashboard/projects/{pid}/monitors/bulk", headers={"accept": "application/json"}, data={
        "sites": "x1.com\nx2.com", "interval_seconds": "60", "timeout_seconds": "10"})
    assert r.status_code == 400 and "100-monitor limit" in r.json()["detail"]


def test_api_key_sees_trial_limits(client):
    from app.services.auth import generate_api_key
    uid = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=5))
    raw, h, prefix = generate_api_key()
    sql("insert into api_keys (id,user_id,name,key_hash,prefix,is_active,created_at) values (:i,:u,'t',:h,:p,true,now())",
        i=uuid.uuid4(), u=uid, h=h, p=prefix)
    pid = make_project(uid, 10)
    r = client.post(f"/projects/{pid}/monitors", headers={"X-API-Key": raw},
                    json={"name": "api", "type": "http", "url": "https://api.example.com", "interval_seconds": 60})
    assert r.status_code == 201, r.text


# ── expiry: pause exactly the right monitors, never delete ──────────────────

def _expiry_fixture():
    """Two projects; monitors created in a mixed order; one paused by the user."""
    uid = make_user(trial_ends=datetime.now(timezone.utc) - timedelta(minutes=5))
    old = make_project(uid, 1000, "Oldest client")
    new = make_project(uid, 500, "Newer client")
    m = {
        "new_a": make_monitor(new, 900),            # oldest monitor overall, but in the newer project
        "old_1": make_monitor(old, 800),
        "old_user_paused": make_monitor(old, 750, active=False),
        "old_2": make_monitor(old, 700, interval=600),
        "old_3": make_monitor(old, 600),
        "old_4": make_monitor(old, 400),
        "new_b": make_monitor(new, 300),
    }
    return uid, old, new, m


def test_expiry_pauses_exactly_the_right_monitors():
    uid, old, new, m = _expiry_fixture()
    before = sql("select count(*) from monitors m join projects p on p.id=m.project_id where p.user_id=:u", u=uid)[0][0]
    out = tasks.trial_housekeeping.run()
    assert out["expired"] >= 1
    # oldest project keeps its 3 oldest RUNNING monitors, interval raised to the Free minimum
    assert mstate(m["old_1"]) == (True, None, 300)
    assert mstate(m["old_2"]) == (True, None, 600)                       # already slower: untouched
    assert mstate(m["old_3"]) == (True, None, 300)
    # everything else paused with the reason; the user's own pause untouched
    for k in ("old_4", "new_a", "new_b"):
        assert mstate(m[k]) == (False, "trial_ended", 60), k
    assert mstate(m["old_user_paused"]) == (False, None, 60)
    # nothing deleted
    after = sql("select count(*) from monitors m join projects p on p.id=m.project_id where p.user_id=:u", u=uid)[0][0]
    assert after == before == 7
    assert urow(uid)[3] is not None                                      # expired marker
    # idempotent: a second run changes nothing
    tasks.trial_housekeeping.run()
    assert mstate(m["old_4"]) == (False, "trial_ended", 60)


def test_banner_after_expiry_and_resume_is_capped(client):
    uid, old, new, m = _expiry_fixture()
    tasks.trial_housekeeping.run()
    login(client, uid)
    html = client.get("/dashboard", headers=BROWSER).text
    assert "Trial ended: 3 monitors paused. Upgrade to resume." in html
    html = client.get(f"/dashboard/projects/{new}", headers=BROWSER).text
    assert 'title="Paused by your plan. Upgrade to resume."' in html
    # resuming a paused monitor past the Free limit is refused
    r = client.post(f"/dashboard/monitors/{m['old_4']}/resume", follow_redirects=False)
    assert "runs%20up%20to%203%20monitors" in r.headers["set-cookie"]
    assert mstate(m["old_4"])[0] is False
    r = client.post(f"/dashboard/monitors/{m['new_b']}/resume", follow_redirects=False)
    assert "covers%201%20project" in r.headers["set-cookie"]
    # but pausing one running monitor frees a slot for another in the same project
    client.post(f"/dashboard/monitors/{m['old_3']}/pause", follow_redirects=False)
    assert mstate(m["old_3"]) == (False, None, 300)                      # now a user pause
    r = client.post(f"/dashboard/monitors/{m['old_4']}/resume", follow_redirects=False)
    assert r.headers["location"] == f"/dashboard/monitors/{m['old_4']}"
    assert mstate(m["old_4"]) == (True, None, 300)                      # resumed at the Free minimum


def test_api_resume_is_capped_too(client):
    from app.services.auth import generate_api_key
    uid, old, new, m = _expiry_fixture()
    tasks.trial_housekeeping.run()
    raw, h, prefix = generate_api_key()
    sql("insert into api_keys (id,user_id,name,key_hash,prefix,is_active,created_at) values (:i,:u,'t',:h,:p,true,now())",
        i=uuid.uuid4(), u=uid, h=h, p=prefix)
    r = client.post(f"/monitors/{m['old_4']}/resume", headers={"X-API-Key": raw})
    assert r.status_code == 403


# ── upgrade resumes them; the webhook clears the trial ──────────────────────

def _webhook(client, monkeypatch, event):
    monkeypatch.setattr(billing.settings, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(billing.settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(billing.settings, "stripe_starter_price_id", "price_starter")
    monkeypatch.setattr(billing.settings, "stripe_pro_price_id", "price_pro")
    monkeypatch.setattr(billing.stripe.Webhook, "construct_event", lambda payload, sig, secret: json.loads(payload))
    return client.post("/billing/webhook", content=json.dumps(event), headers={"stripe-signature": "t=1,v1=x"})


def _sub_event(customer, price, status="active", etype="customer.subscription.updated"):
    return {"type": etype, "data": {"object": {
        "id": "sub_" + customer, "customer": customer, "status": status,
        "current_period_end": int(time.time()) + 30 * 86400,
        "items": {"data": [{"price": {"id": price}}]}}}}


def test_upgrade_after_expiry_resumes_up_to_new_limits(client, monkeypatch):
    uid, old, new, m = _expiry_fixture()
    tasks.trial_housekeeping.run()
    cust = "cus_" + uid.hex[:12]
    sql("update users set stripe_customer_id=:c where id=:i", c=cust, i=uid)
    r = _webhook(client, monkeypatch, _sub_event(cust, "price_starter"))
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    plan, ends, used, _, _ = urow(uid)
    assert plan == "STARTER" and ends is None and used is True
    for k in ("old_1", "old_2", "old_3", "old_4", "new_a", "new_b"):
        active, reason, _ = mstate(m[k])
        assert active is True and reason is None, k
    assert mstate(m["old_user_paused"]) == (False, None, 60)             # the user's choice stands
    login(client, uid)
    assert "Trial ended" not in client.get("/dashboard", headers=BROWSER).text


def test_upgrade_resumes_only_up_to_the_paid_limit(client, monkeypatch):
    uid = make_user(trial_ends=datetime.now(timezone.utc) - timedelta(minutes=1))
    pids = [make_project(uid, 2000 - i) for i in range(12)]
    mids = [make_monitor(p, 1500 - j, interval=60) for j, p in enumerate(pids * 4)]   # 48 monitors
    tasks.trial_housekeeping.run()
    assert sum(mstate(x)[0] for x in mids) == 3
    cust = "cus_" + uid.hex[:12]
    sql("update users set stripe_customer_id=:c where id=:i", c=cust, i=uid)
    _webhook(client, monkeypatch, _sub_event(cust, "price_starter"))     # 10 projects, 40 monitors
    running = [x for x in mids if mstate(x)[0]]
    assert len(running) == 40
    allowed = set(pids[:10])
    assert all(sql("select project_id from monitors where id=:i", i=x)[0][0] in allowed for x in running)
    assert all(mstate(x)[1] == "trial_ended" for x in mids if not mstate(x)[0])


def test_webhook_during_trial_clears_it_and_keeps_monitors(client, monkeypatch):
    uid = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=6))
    pid = make_project(uid, 100)
    mids = [make_monitor(pid, 90 - i) for i in range(20)]
    cust = "cus_" + uid.hex[:12]
    sql("update users set stripe_customer_id=:c where id=:i", c=cust, i=uid)
    r = _webhook(client, monkeypatch, _sub_event(cust, "price_pro", etype="customer.subscription.created"))
    assert r.status_code == 200
    plan, ends, used, _, _ = urow(uid)
    assert plan == "PRO" and ends is None and used is True
    assert all(mstate(x)[0] for x in mids)
    # cancelling later drops to Free, not back into a trial; Free cannot restart it
    r = _webhook(client, monkeypatch, {"type": "customer.subscription.deleted", "data": {"object": {"customer": cust}}})
    assert r.status_code == 200
    plan, ends, used, _, _ = urow(uid)
    assert plan == "FREE" and ends is None and used is True
    login(client, uid)
    r = client.post("/dashboard/billing/trial", headers={"accept": "application/json"})
    assert r.status_code == 409


def test_webhooks_without_a_trial_are_unchanged(client, monkeypatch):
    uid = make_user()
    pid = make_project(uid, 100)
    user_paused = make_monitor(pid, 50, active=False)
    cust = "cus_" + uid.hex[:12]
    sql("update users set stripe_customer_id=:c where id=:i", c=cust, i=uid)
    r = _webhook(client, monkeypatch, _sub_event(cust, "price_pro"))
    assert r.status_code == 200 and urow(uid)[0] == "PRO"
    assert mstate(user_paused) == (False, None, 60)
    r = _webhook(client, monkeypatch, {"type": "invoice.payment_failed", "data": {"object": {"customer": cust}}})
    assert r.status_code == 200
    assert sql("select subscription_status from users where id=:i", i=uid)[0][0] == "past_due"


# ── reminder email: once, 3 days out, transactional mailer ──────────────────

def test_one_reminder_three_days_before_the_end(monkeypatch):
    from app.services import mailer
    sent = []
    monkeypatch.setattr(mailer, "send_sync", lambda to, subject, text, html=None, kind="": sent.append((to, subject, kind)))
    soon = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=2, hours=20))
    later = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=9))
    unverified = make_user(verified=False, trial_ends=datetime.now(timezone.utc) + timedelta(days=2))
    tasks.trial_housekeeping.run()
    emails = {e for e, _, _ in sent}
    soon_email = sql("select email from users where id=:i", i=soon)[0][0]
    assert soon_email in emails
    assert sql("select email from users where id=:i", i=later)[0][0] not in emails
    assert sql("select email from users where id=:i", i=unverified)[0][0] not in emails
    assert all(k == "trial reminder" and "3 days" in s for _, s, k in sent)
    assert urow(soon)[4] is not None
    n = len(sent)
    tasks.trial_housekeeping.run()
    assert len(sent) == n                                               # only once


def test_reminder_retries_after_a_provider_error(monkeypatch):
    from app.services import mailer
    uid = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=1))

    def boom(*a, **k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(mailer, "send_sync", boom)
    tasks.trial_housekeeping.run()
    assert urow(uid)[4] is None
    sent = []
    monkeypatch.setattr(mailer, "send_sync", lambda *a, **k: sent.append(a[0]))
    tasks.trial_housekeeping.run()
    assert urow(uid)[4] is not None and sent


# ── site-check arrivals get the offer on their first project ────────────────

def test_site_check_signup_gets_trial_offer_on_project_screen(client):
    email = f"trial-sc-{uuid.uuid4().hex[:8]}@eramirez.dev"
    client.cookies.clear()
    r = client.post("/dashboard/register", follow_redirects=False, headers=BROWSER, data={
        "email": email, "password": "hunter2hunter2", "confirm_password": "hunter2hunter2", "ref": "site-check"})
    assert r.status_code == 303
    r = client.post("/dashboard/projects", data={"name": "Client sites"}, follow_redirects=False)
    pid = r.headers["location"].rsplit("/", 1)[1]
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert 'id="trial-offer"' in html and "Try Pro free for 14 days, no card" in html
    client.post("/dashboard/billing/trial", follow_redirects=False,
                headers={"referer": f"https://testserver/dashboard/projects/{pid}"})
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert 'id="trial-offer"' not in html and "Pro trial: 14 days left" in html
    # an ordinary signup does not get the project-screen offer
    email2 = f"trial-plain-{uuid.uuid4().hex[:8]}@eramirez.dev"
    client.cookies.clear()
    client.post("/dashboard/register", follow_redirects=False, headers=BROWSER, data={
        "email": email2, "password": "hunter2hunter2", "confirm_password": "hunter2hunter2", "ref": "direct"})
    r = client.post("/dashboard/projects", data={"name": "Mine"}, follow_redirects=False)
    html = client.get(r.headers["location"], headers=BROWSER).text
    assert 'id="trial-offer"' not in html
    assert "Try Pro free" in client.get("/dashboard/billing", headers=BROWSER).text


def test_dismissing_the_offer(client):
    uid = make_user()
    sql("update users set trial_offer=true where id=:i", i=uid)
    pid = make_project(uid, 5)
    login(client, uid)
    assert 'id="trial-offer"' in client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    r = client.post("/dashboard/billing/trial/dismiss", follow_redirects=False,
                    headers={"referer": f"https://testserver/dashboard/projects/{pid}"})
    assert r.headers["location"] == f"/dashboard/projects/{pid}"
    assert 'id="trial-offer"' not in client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert urow(uid)[2] is False                                         # dismissing is not using


# ── migration round trip ────────────────────────────────────────────────────

def test_migration_down_and_up_round_trip():
    env = {**os.environ}
    alembic = [sys.executable, "-m", "alembic"]

    def cols():
        return {r[0] for r in sql("select column_name from information_schema.columns where table_name in ('users','monitors')")}
    assert {"trial_ends_at", "trial_used", "paused_reason"} <= cols()
    uid = make_user(trial_ends=datetime.now(timezone.utc) + timedelta(days=1))
    down = subprocess.run(alembic + ["downgrade", "e3c1a7f05d92"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert down.returncode == 0, down.stderr[-800:]
    assert not ({"trial_ends_at", "trial_used", "trial_offer", "trial_reminder_sent_at",
                 "trial_expired_at", "paused_reason"} & cols())
    assert sql("select count(*) from users where id=:i", i=uid)[0][0] == 1   # data survives
    up = subprocess.run(alembic + ["upgrade", "head"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert up.returncode == 0, up.stderr[-800:]
    assert {"trial_ends_at", "trial_used", "trial_offer", "trial_reminder_sent_at",
            "trial_expired_at", "paused_reason"} <= cols()
    assert sql("select trial_used from users where id=:i", i=uid)[0][0] is False   # server default
