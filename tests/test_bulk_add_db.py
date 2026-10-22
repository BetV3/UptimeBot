"""Bulk add and the public site check, on real Postgres + Redis (scripts/test_agent_api.sh).

Site check: fake regional workers claim the throwaway checks through the real
/internal/jobs and post through the real /internal/results, like the real ones.
"""
import os
import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("CP_AGENT_API_TEST"),
                                reason="needs scripts/test_agent_api.sh (real Postgres + Redis)")

if os.environ.get("CP_AGENT_API_TEST"):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from app.api.routes import tools
    from app.core.config import get_settings
    from app.main import app
    from app.services import regions as region_svc
    from app.services import site_check, target_guard
    from app.services.auth import create_access_token
    from app.workers import tasks

    SET = get_settings()
    ENG = create_engine(SET.database_url_sync)

BROWSER = {"user-agent": "Mozilla/5.0 Chrome/128", "accept": "text/html"}


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
def clean_redis(monkeypatch):
    import redis
    redis.Redis.from_url(SET.redis_url).flushdb()
    tools._redis_client = None
    # Celery is not running in tests: run consensus inline so its effects are real.
    monkeypatch.setattr(tasks.finalize_check_result, "delay", lambda mid: tasks.finalize_check_result.run(mid))
    # Example names must "resolve" publicly without the network.
    monkeypatch.setattr(target_guard, "resolve", lambda h: [] if h.endswith(".invalid") else ["93.184.215.14"])
    yield


def make_user(plan="FREE"):
    uid, pid = uuid.uuid4(), uuid.uuid4()
    sql("insert into users (id,email,password_hash,plan,email_verified,created_at) "
        "values (:i,:e,'x',:p,true,now())", i=uid, e=f"bulk-{uid.hex[:10]}@eramirez.dev", p=plan)
    sql("insert into projects (id,user_id,name,slug,created_at) values (:i,:u,'p',:s,now())",
        i=pid, u=uid, s=f"p-{uid.hex[:12]}")
    return uid, str(pid)


def login(c, uid):
    c.cookies.clear()
    c.cookies.set("access_token", create_access_token(str(uid)))


def monitors(pid):
    return sql("select name, type, url, interval_seconds, target_host from monitors "
               "where project_id=:p order by created_at, name", p=pid)


def bulk(c, pid, sites, interval="300", also_ssl=False, json=True):
    data = {"sites": sites, "interval_seconds": interval, "timeout_seconds": "10"}
    if also_ssl:
        data["also_ssl"] = "1"
    return c.post(f"/dashboard/projects/{pid}/monitors/bulk", data=data, follow_redirects=False,
                  headers={"accept": "application/json"} if json else {})


# ── bulk add ─────────────────────────────────────────────────────────────────

def test_bulk_add_creates_one_http_monitor_per_site(client):
    uid, pid = make_user("PRO")
    login(client, uid)
    r = bulk(client, pid, "alpha.com\nhttps://www.beta.com/shop\nalpha.com")
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2
    rows = monitors(pid)
    assert {(n, t, u) for n, t, u, _, _ in rows} == {
        ("alpha.com", "HTTP", "https://alpha.com"), ("beta.com/shop", "HTTP", "https://www.beta.com/shop")}
    # first checks are staggered, not all at once
    times = sql("select next_check_at from monitors where project_id=:p order by next_check_at", p=pid)
    assert times[0][0] != times[1][0]


def test_bulk_add_with_ssl_adds_hourly_cert_monitors(client):
    uid, pid = make_user("PRO")
    login(client, uid)
    r = bulk(client, pid, "alpha.com\nhttp://plain.com", also_ssl=True)
    assert r.json()["created"] == 3, r.text      # 2 HTTP + 1 SSL (plain http has no cert)
    ssl = [row for row in monitors(pid) if row[1] == "SSL"]
    assert len(ssl) == 1 and ssl[0][4] == "alpha.com" and ssl[0][3] == 3600


def test_bulk_add_skips_existing_and_reports_internal_and_junk(client):
    uid, pid = make_user("PRO")
    login(client, uid)
    bulk(client, pid, "alpha.com")
    r = bulk(client, pid, "alpha.com/\nbeta.com\nlocalhost\nhttp://169.254.169.254/latest\nnot a site")
    assert r.json()["created"] == 1, r.text
    msg = r.json()["message"]
    assert "already monitored" in msg and "internal" in msg
    assert [row[0] for row in monitors(pid)].count("alpha.com") == 1


def test_bulk_add_stops_at_plan_limit_and_says_so(client):
    uid, pid = make_user("FREE")             # 3 monitors
    login(client, uid)
    r = bulk(client, pid, "a1.com\na2.com\na3.com\na4.com\na5.com")
    assert r.json()["created"] == 3 and r.json()["skipped"] == 2
    assert "3-monitor limit" in r.json()["message"]
    assert len(monitors(pid)) == 3


def test_bulk_add_http_first_when_limit_is_tight(client):
    uid, pid = make_user("FREE")
    login(client, uid)
    r = bulk(client, pid, "a1.com\na2.com", also_ssl=True)
    assert sorted(t for _, t, _, _, _ in monitors(pid)) == ["HTTP", "HTTP", "SSL"]
    assert "a2.com SSL" in r.json()["message"]


def test_bulk_add_validates_interval_and_input(client):
    uid, pid = make_user("FREE")
    login(client, uid)
    assert bulk(client, pid, "a.com", interval="60").status_code == 403      # FREE min is 300
    assert bulk(client, pid, "").status_code == 400
    assert bulk(client, pid, "\n".join(f"s{i}.com" for i in range(101))).status_code == 400
    r = bulk(client, pid, "localhost", json=False)
    assert r.status_code == 303 and "flash_error" in r.headers.get("set-cookie", "")
    assert monitors(pid) == []


def test_bulk_add_needs_login_and_own_project(client):
    uid, pid = make_user("PRO")
    other, other_pid = make_user("PRO")
    client.cookies.clear()
    r = bulk(client, pid, "a.com", json=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
    login(client, uid)
    assert bulk(client, other_pid, "a.com").status_code == 404
    assert monitors(other_pid) == []


def test_bulk_add_names_never_collide(client):
    uid, pid = make_user("PRO")
    login(client, uid)
    sql("insert into monitors (id,project_id,name,type,url,method,expected_status,interval_seconds,"
        "timeout_seconds,is_active,current_status,created_at) values (:i,:p,'alpha.com','HTTP',"
        "'https://other.example/','GET',200,300,10,true,'UNKNOWN',now())", i=uuid.uuid4(), p=pid)
    r = bulk(client, pid, "alpha.com")
    assert r.json()["created"] == 1
    assert "alpha.com (2)" in [row[0] for row in monitors(pid)]


def test_project_page_shows_bulk_add(client):
    uid, pid = make_user("PRO")
    login(client, uid)
    html = client.get(f"/dashboard/projects/{pid}", headers=BROWSER).text
    assert 'id="bulk-add-modal"' in html and f"/dashboard/projects/{pid}/monitors/bulk" in html


# ── site check ───────────────────────────────────────────────────────────────

@pytest.fixture
def us_regions():
    """Exactly three fresh, guarded workers: us-east, us-central, us-west."""
    before = sql("select code, scheduled, voting from regions")
    hb = sql("select region, version, last_seen from worker_heartbeats")
    sql("update regions set scheduled = code in ('us-east','us-central','us-west'), voting = false")
    sql("delete from worker_heartbeats")
    for code in ("us-east", "us-central", "us-west"):
        sql("insert into worker_heartbeats (id,region,version,last_seen) values (:i,:r,'1.1.0',now())",
            i=uuid.uuid4(), r=code)
    region_svc.invalidate_cache()
    yield ("us-central", "us-east", "us-west")
    sql("delete from worker_heartbeats")
    for code, ver, seen in hb:
        sql("insert into worker_heartbeats (id,region,version,last_seen) values (:i,:r,:v,:s)",
            i=uuid.uuid4(), r=code, v=ver, s=seen)
    for code, s, v in before:
        sql("update regions set scheduled=:s, voting=:v where code=:c", s=s, v=v, c=code)
    region_svc.invalidate_cache()


@pytest.fixture
def no_ssl(monkeypatch):
    from app.services.ssl_probe import ProbeError

    def fake(h):
        raise ProbeError("test: no TLS here")
    monkeypatch.setattr(tools, "probe", fake)


def _fake_workers(c, outcome, stop, regions):
    """outcome(region, url) -> (status, status_code)."""
    def loop(region):
        hdr = {"X-Worker-Secret": SET.worker_secret, "X-Worker-Id": f"test-{region}"}
        while not stop.is_set():
            jobs = c.get(f"/internal/jobs?region={region}", headers=hdr).json()["jobs"]
            if jobs:
                res = []
                for j in jobs:
                    st, code = outcome(region, j["url"])
                    res.append({"pending_check_id": j["pending_check_id"], "monitor_id": j["monitor_id"],
                                "region": region, "status": st, "status_code": code, "response_time_ms": 50})
                assert c.post("/internal/results", headers=hdr, json={"results": res}).status_code == 200
            time.sleep(0.2)
    ts = [threading.Thread(target=loop, args=(r,), daemon=True) for r in regions]
    for t in ts:
        t.start()
    return ts


def _run(c, sites):
    r = c.post("/tools/site-check", data={"sites": sites}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 303, r.text[:400]
    return r.headers["location"]


def test_site_check_end_to_end_with_blocked_location(client, us_regions, no_ssl):
    def outcome(region, url):
        if "blocky" in url and region == "us-west":
            return "down", 403
        return "up", 200
    stop = threading.Event()
    ts = _fake_workers(client, outcome, stop, us_regions)
    try:
        loc = _run(client, "fine.com\nblocky.com")
        for _ in range(60):
            html = client.get(loc, headers=BROWSER).text
            if "// results" in html:
                break
            time.sleep(0.3)
    finally:
        stop.set()
        for t in ts:
            t.join(timeout=5)
    assert "// results" in html
    assert "blocked from some locations" in html and "blocked (403)" in html
    assert 'content="noindex"' in html and "/dashboard/register?ref=site-check" in html
    # throwaway monitors are gone, nothing opened an incident, nothing is scheduled again
    sys_pid = sql("select id from projects where slug=:s", s=site_check.SYSTEM_SLUG)[0][0]
    assert sql("select count(*) from monitors where project_id=:p", p=sys_pid)[0][0] == 0
    assert sql("select count(*) from incidents i join monitors m on m.id=i.monitor_id "
               "where m.project_id=:p", p=sys_pid)[0][0] == 0
    # the shared link keeps working after cleanup
    assert "blocked (403)" in client.get(loc, headers=BROWSER).text


def test_site_check_is_never_sent_to_old_or_dead_workers(client, us_regions, no_ssl):
    sql("update worker_heartbeats set version='1.0.0' where region='us-east'")
    sql("update worker_heartbeats set last_seen=now() - interval '10 minutes' where region='us-west'")
    loc = _run(client, "fine.com")
    rid = loc.rsplit("/", 1)[1]
    import json as _j, redis
    run = _j.loads(redis.Redis.from_url(SET.redis_url).get("cp:sitecheck:run:" + rid))
    assert [r["code"] for r in run["regions"]] == ["us-central"]
    mid = run["sites"][0]["monitor_id"]
    assert [r[0] for r in sql("select region from pending_checks where monitor_id=:m", m=mid)] == ["us-central"]


def test_site_check_refuses_internal_targets(client, us_regions, no_ssl):
    before = sql("select count(*) from pending_checks")[0][0]
    for target in ("localhost", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/"):
        r = client.post("/tools/site-check", data={"sites": target}, headers=BROWSER, follow_redirects=False)
        assert r.status_code == 400, target
    assert sql("select count(*) from pending_checks")[0][0] == before


def test_site_check_limits_fail_closed_and_cap_sites(client, us_regions, no_ssl, monkeypatch):
    r = client.post("/tools/site-check", data={"sites": "\n".join(f"s{i}.com" for i in range(11))},
                    headers=BROWSER, follow_redirects=False)
    assert r.status_code == 400 and "up to 10" in r.text
    for _ in range(tools.SITE_PER_MINUTE):
        _run(client, "fine.com")
    r = client.post("/tools/site-check", data={"sites": "fine.com"}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 429

    def down():
        raise ConnectionError("redis down")
    monkeypatch.setattr(tools, "_redis", down)
    r = client.post("/tools/site-check", data={"sites": "fine.com"}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 429 and "unavailable" in r.text


def test_site_check_global_budget_counts_sites_not_runs(client, us_regions, no_ssl, monkeypatch):
    monkeypatch.setattr(tools, "SITE_GLOBAL_SITES_PER_HOUR", 12)
    monkeypatch.setattr(tools, "visitor_key", lambda req: f"v-{uuid.uuid4().hex[:8]}")  # many visitors
    _run(client, "\n".join(f"s{i}.com" for i in range(10)))
    r = client.post("/tools/site-check", data={"sites": "a.com\nb.com\nc.com"}, headers=BROWSER,
                    follow_redirects=False)
    assert r.status_code == 429 and "busy" in r.text


def test_site_check_bad_and_expired_links(client):
    assert client.get("/tools/site-check/r/x", headers=BROWSER).status_code == 404
    r = client.get("/tools/site-check/r/AAAAAAAAAAAAAAAA", headers=BROWSER)
    assert r.status_code == 404 and "expired" in r.text


def test_sweep_removes_abandoned_runs(client, us_regions, no_ssl):
    loc = _run(client, "fine.com")
    sys_pid = sql("select id from projects where slug=:s", s=site_check.SYSTEM_SLUG)[0][0]
    sql("update monitors set created_at = now() - interval '1 hour' where project_id=:p", p=sys_pid)
    _run(client, "other.com")            # every new run sweeps first
    names = [r[0] for r in sql("select url from monitors where project_id=:p", p=sys_pid)]
    assert names == ["https://other.com"], names
    assert loc
    # and the reaper sweeps too, even if nobody runs another check
    sql("update monitors set created_at = now() - interval '1 hour' where project_id=:p", p=sys_pid)
    assert tasks.reap_dead_pending_checks.run()["site_check_swept"] == 1
    assert sql("select count(*) from monitors where project_id=:p", p=sys_pid)[0][0] == 0


def test_late_result_for_deleted_monitor_does_not_fail_batch(client, us_regions):
    uid, pid = make_user("PRO")
    login(client, uid)
    bulk(client, pid, "keep.com")
    keep = sql("select id from monitors where project_id=:p", p=pid)[0][0]
    pc = uuid.uuid4()
    sql("insert into pending_checks (id,monitor_id,region,scheduled_at,worker_id) values (:i,:m,'us-east',now(),'w1')",
        i=pc, m=keep)
    hdr = {"X-Worker-Secret": SET.worker_secret, "X-Worker-Id": "w1"}
    res = [{"pending_check_id": str(pc), "monitor_id": str(keep), "region": "us-east", "status": "up",
            "status_code": 200, "response_time_ms": 10},
           {"pending_check_id": str(uuid.uuid4()), "monitor_id": str(uuid.uuid4()), "region": "us-east",
            "status": "up", "status_code": 200, "response_time_ms": 10}]
    r = client.post("/internal/results", headers=hdr, json={"results": res})
    assert r.status_code == 200 and r.json() == {"saved": 1}
    assert sql("select count(*) from checks where monitor_id=:m", m=keep)[0][0] == 1


def test_site_check_form_page_is_public_and_indexable(client):
    r = client.get("/tools/site-check", headers=BROWSER)
    assert r.status_code == 200 and 'content="noindex"' not in r.text
    assert "https://checkpulse.dev/tools/site-check" in r.text


def test_site_check_no_workers_is_503(client, us_regions, no_ssl):
    sql("update worker_heartbeats set last_seen=now() - interval '1 hour'")
    r = client.post("/tools/site-check", data={"sites": "fine.com"}, headers=BROWSER, follow_redirects=False)
    assert r.status_code == 503


def test_site_check_ssl_days_survive_finalize(client, us_regions, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.services.ssl_probe import CertReport
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(tools, "probe", lambda h: CertReport(
        host=h, trusted=True, trust_error=None, subject_cn=h, issuer="Let's Encrypt",
        not_before=now - timedelta(days=60), not_after=now + timedelta(days=9, hours=1)))
    stop = threading.Event()
    ts = _fake_workers(client, lambda r, u: ("up", 200), stop, us_regions)
    try:
        loc = _run(client, "fine.com")
        for _ in range(60):
            html = client.get(loc, headers=BROWSER).text
            if "// results" in html and "9 days" in html:
                break
            time.sleep(0.3)
    finally:
        stop.set()
        for t in ts:
            t.join(timeout=5)
    assert "// results" in html and "9 days" in html
    assert "9 days" in client.get(loc, headers=BROWSER).text
