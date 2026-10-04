"""Agent API: REST monitor types, check-now, API keys on the account page.

Runs against a real Postgres + Redis (scripts/test_agent_api.sh starts
throwaway containers and runs migrations). Skipped otherwise, because the
whole point is the real lease path: check-now writes pending_checks, and a
fake regional worker claims them through the real /internal/jobs and posts
through the real /internal/results, exactly like the US/EU/Asia workers.
"""
import asyncio
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

    from app.api.routes import monitors as monitors_routes
    from app.core.config import get_settings
    from app.main import app
    from app.services.auth import generate_api_key

    SET = get_settings()
    ENG = create_engine(SET.database_url_sync)


def sql(q, **kw):
    with ENG.begin() as c:
        r = c.execute(text(q), kw)
        try:
            return r.fetchall()
        except Exception:
            return None


def make_user(plan="FREE"):
    uid = uuid.uuid4()
    email = f"agentapi-{uid.hex[:10]}@eramirez.dev"
    sql("insert into users (id,email,password_hash,plan,email_verified,created_at) "
        "values (:i,:e,'x',:p,true,now())", i=uid, e=email, p=plan)
    raw, h, prefix = generate_api_key()
    sql("insert into api_keys (id,user_id,name,key_hash,prefix,is_active,created_at) "
        "values (:i,:u,'t',:h,:p,true,now())", i=uuid.uuid4(), u=uid, h=h, p=prefix)
    pid = uuid.uuid4()
    sql("insert into projects (id,user_id,name,slug,created_at) values (:i,:u,'p',:s,now())",
        i=pid, u=uid, s=f"p-{uid.hex[:12]}")
    return uid, raw, str(pid)


@pytest.fixture(scope="module")
def client():
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def flush_redis():
    import redis
    redis.Redis.from_url(SET.redis_url).flushdb()
    monitors_routes._check_now_sem = asyncio.Semaphore(monitors_routes.CHECK_NOW_CONCURRENCY)
    yield


def H(key):
    return {"X-API-Key": key}


# ── create: every monitor type, same validators as the dashboard ──────────

def test_create_http_ssl_dns_and_list(client):
    _, key, pid = make_user()
    r = client.post(f"/projects/{pid}/monitors", headers=H(key),
                    json={"name": "site", "type": "http", "url": "https://example.com", "interval_seconds": 300})
    assert r.status_code == 201, r.text
    assert r.json()["type"] == "http"
    r = client.post(f"/projects/{pid}/monitors", headers=H(key),
                    json={"name": "cert", "type": "ssl", "target_host": "example.com", "interval_seconds": 300})
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["type"] == "ssl" and j["target_host"] == "example.com" and j["target_port"] == 443
    r = client.post(f"/projects/{pid}/monitors", headers=H(key),
                    json={"name": "dns", "type": "dns", "target_host": "example.com",
                          "dns_record_type": "A", "dns_expected_value": "93.184.215.14", "interval_seconds": 300})
    assert r.status_code == 201, r.text
    assert r.json()["dns_record_type"] == "A"
    lst = client.get(f"/projects/{pid}/monitors", headers=H(key)).json()
    assert sorted(m["type"] for m in lst) == ["dns", "http", "ssl"]


@pytest.mark.parametrize("body,needle", [
    ({"name": "x", "type": "ftp", "url": "https://example.com"}, "type must be"),
    ({"name": "x", "type": "http", "url": "ftp://example.com"}, "http:// or https://"),
    ({"name": "", "type": "http", "url": "https://example.com"}, "blank"),
    ({"name": "x", "type": "ssl", "target_host": ""}, "blank"),
    ({"name": "x", "type": "dns", "target_host": "example.com", "dns_expected_value": ""}, "blank"),
    ({"name": "x", "type": "http", "url": "https://example.com", "timeout_seconds": 400}, "Timeout"),
])
def test_create_rejects_what_the_form_rejects(client, body, needle):
    _, key, pid = make_user()
    body = {"interval_seconds": 300, **body}
    r = client.post(f"/projects/{pid}/monitors", headers=H(key), json=body)
    assert r.status_code == 400, r.text
    assert needle.lower() in r.json()["detail"].lower()


def test_duplicate_name_and_plan_limits(client):
    _, key, pid = make_user("FREE")
    ok = {"name": "a", "type": "http", "url": "https://example.com", "interval_seconds": 300}
    assert client.post(f"/projects/{pid}/monitors", headers=H(key), json=ok).status_code == 201
    assert client.post(f"/projects/{pid}/monitors", headers=H(key), json=ok).status_code == 409
    fast = {**ok, "name": "b", "interval_seconds": 60}
    assert client.post(f"/projects/{pid}/monitors", headers=H(key), json=fast).status_code == 403
    for n in ("c", "d"):
        assert client.post(f"/projects/{pid}/monitors", headers=H(key), json={**ok, "name": n}).status_code == 201
    r = client.post(f"/projects/{pid}/monitors", headers=H(key), json={**ok, "name": "e"})
    assert r.status_code == 403 and "up to 3" in r.json()["detail"]


def test_other_users_project_is_404(client):
    _, key_a, _ = make_user()
    _, _, pid_b = make_user()
    r = client.post(f"/projects/{pid_b}/monitors", headers=H(key_a),
                    json={"name": "x", "url": "https://example.com", "interval_seconds": 300})
    assert r.status_code == 404


def test_bad_key_is_401(client):
    r = client.get("/projects", headers=H("ub_not_a_real_key_000000000000"))
    assert r.status_code == 401


def test_no_auth_is_401_and_key_alone_works(client):
    """Regression: OAuth2PasswordBearer(auto_error=True) 401'd every request
    that had X-API-Key but no Authorization header, so keys never worked."""
    client.cookies.clear()
    assert client.get("/projects").status_code == 401
    uid, key, _ = make_user()
    r = client.get("/projects", headers=H(key))
    assert r.status_code == 200, r.text
    used = sql("select last_used_at from api_keys where user_id=:u", u=uid)[0][0]
    assert used is not None


# ── check-now: real lease path, fake regional workers ───────────────────────

def _worker(client, region, status_map, stop):
    hdr = {"X-Worker-Secret": SET.worker_secret, "X-Worker-Id": f"test-{region}"}
    while not stop.is_set():
        jobs = client.get(f"/internal/jobs?region={region}", headers=hdr).json()["jobs"]
        if jobs:
            res = [{"pending_check_id": j["pending_check_id"], "monitor_id": j["monitor_id"],
                    "region": region, "status": status_map[region], "status_code": 200,
                    "response_time_ms": 42} for j in jobs]
            client.post("/internal/results", headers=hdr, json={"results": res})
        time.sleep(0.3)


def _with_workers(client, status_map, fn, regions=("us", "eu", "asia")):
    stop = threading.Event()
    ts = [threading.Thread(target=_worker, args=(client, r, status_map, stop), daemon=True) for r in regions]
    for t in ts:
        t.start()
    try:
        return fn()
    finally:
        stop.set()
        for t in ts:
            t.join(timeout=5)


def _monitor(client, key, pid, name="m"):
    r = client.post(f"/projects/{pid}/monitors", headers=H(key),
                    json={"name": name, "url": "https://example.com", "interval_seconds": 300})
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    # keep the scheduler out of it: only check-now may create pending_checks
    sql("update monitors set next_check_at = now() + interval '1 day' where id=:i", i=mid)
    return mid


def test_check_now_all_up(client):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)
    r = _with_workers(client, {"us": "up", "eu": "up", "asia": "up"},
                      lambda: client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 20}))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["verdict"] == "up" and j["regions_missing"] == []
    assert sorted(x["region"] for x in j["results"]) == ["asia", "eu", "us"]
    # it is real history, not a side channel
    n = sql("select count(*) from checks where monitor_id=:i", i=mid)[0][0]
    assert n == 3


def test_check_now_two_of_three_down_is_down(client):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)
    r = _with_workers(client, {"us": "down", "eu": "down", "asia": "up"},
                      lambda: client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 20}))
    assert r.json()["verdict"] == "down", r.text


def test_check_now_one_of_three_down_is_up(client):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)
    r = _with_workers(client, {"us": "down", "eu": "up", "asia": "up"},
                      lambda: client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 20}))
    assert r.json()["verdict"] == "up", r.text


def test_check_now_missing_region_reports_it(client):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)
    r = _with_workers(client, {"us": "up", "eu": "down"},
                      lambda: client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 6}),
                      regions=("us", "eu"))
    j = r.json()
    assert j["verdict"] == "incomplete" and j["regions_missing"] == ["asia"], j


def test_check_now_rate_limits(client):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)
    first = _with_workers(client, {"us": "up", "eu": "up", "asia": "up"},
                          lambda: client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 20}))
    assert first.status_code == 200
    again = client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 5})
    assert again.status_code == 429 and "20s" in again.json()["detail"]


def test_check_now_per_user_hourly_cap(client, monkeypatch):
    uid, key, pid = make_user()
    mid = _monitor(client, key, pid)
    import redis
    redis.Redis.from_url(SET.redis_url).set(f"cp:checknow:u:{uid}", monitors_routes.CHECK_NOW_PER_HOUR, ex=3600)
    r = client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 5})
    assert r.status_code == 429 and "per hour" in r.json()["detail"]


def test_check_now_fails_closed_without_redis(client, monkeypatch):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)

    class Dead:
        async def set(self, *a, **k):
            raise ConnectionError("down")
    from app.api.routes import tools
    monkeypatch.setattr(tools, "_redis", lambda: Dead())
    r = client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 5})
    assert r.status_code == 429 and "unavailable" in r.json()["detail"]
    assert sql("select count(*) from pending_checks where monitor_id=:i", i=mid)[0][0] == 0


def test_check_now_other_users_monitor_is_404(client):
    _, key_a, _ = make_user()
    _, key_b, pid_b = make_user()
    mid_b = _monitor(client, key_b, pid_b)
    r = client.post(f"/monitors/{mid_b}/check-now", headers=H(key_a), json={"wait_seconds": 5})
    assert r.status_code == 404
    assert sql("select count(*) from pending_checks where monitor_id=:i", i=mid_b)[0][0] == 0


def test_wait_seconds_bounds(client):
    _, key, pid = make_user()
    mid = _monitor(client, key, pid)
    assert client.post(f"/monitors/{mid}/check-now", headers=H(key), json={"wait_seconds": 500}).status_code == 422


# ── API keys on the account page ───────────────────────────────────────────

def _login_cookie(uid):
    from app.services.auth import create_access_token
    return {"access_token": create_access_token(str(uid))}


def test_account_create_shows_key_once_and_it_works(client):
    uid, _, _ = make_user()
    client.cookies.clear()
    r = client.post("/dashboard/account/api-keys", data={"name": "Claude"}, cookies=_login_cookie(uid),
                    follow_redirects=False)
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-store"
    import re
    m = re.search(r"(ub_[A-Za-z0-9_\-]{20,})", r.text)
    assert m, "new key must be shown in the page"
    raw = m.group(1)
    assert client.get("/projects", headers=H(raw)).status_code == 200
    # only the hash is stored
    assert sql("select count(*) from api_keys where key_hash = :k", k=raw)[0][0] == 0
    page = client.get("/dashboard/account", cookies=_login_cookie(uid)).text
    assert raw not in page and "Claude" in page


def test_account_revoke_kills_key_and_only_own(client):
    uid, key, _ = make_user()
    uid2, key2, _ = make_user()
    kid2 = sql("select id from api_keys where user_id=:u", u=uid2)[0][0]
    r = client.post(f"/dashboard/account/api-keys/{kid2}/revoke", cookies=_login_cookie(uid), follow_redirects=False)
    assert r.status_code == 404
    assert client.get("/projects", headers=H(key2)).status_code == 200
    kid = sql("select id from api_keys where user_id=:u", u=uid)[0][0]
    r = client.post(f"/dashboard/account/api-keys/{kid}/revoke", cookies=_login_cookie(uid), follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/projects", headers=H(key)).status_code == 401


def test_account_api_keys_need_login(client):
    client.cookies.clear()
    r = client.post("/dashboard/account/api-keys", data={"name": "x"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard/login"
