"""CP-1 regions: shadow regions, the stale-vote fix, unknown regions, flags at runtime.

Real Postgres + Redis (scripts/test_agent_api.sh). Each test that changes the
regions table restores it, and clears the 60 s in-process region cache.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("CP_AGENT_API_TEST"),
                                reason="needs scripts/test_agent_api.sh (real Postgres + Redis)")

if os.environ.get("CP_AGENT_API_TEST"):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from app.core.config import get_settings
    from app.main import app
    from app.services import regions as region_svc
    from app.workers import tasks

    SET = get_settings()
    ENG = create_engine(SET.database_url_sync)


def sql(q, **kw):
    with ENG.begin() as c:
        r = c.execute(text(q), kw)
        try:
            return r.fetchall()
        except Exception:
            return None


@pytest.fixture(scope="module")
def client():
    # The async engine is module-global; its pooled asyncpg connections belong to
    # the event loop of whichever TestClient ran first (test_agent_api.py). Drop
    # them so this module's TestClient loop opens its own.
    from app.core.database import engine
    engine.sync_engine.dispose(close=False)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def regions_snapshot():
    """Restore the regions flags after the test, whatever it did."""
    before = sql("select code, scheduled, voting from regions")
    region_svc.invalidate_cache()
    yield
    for code, s, v in before:
        sql("update regions set scheduled=:s, voting=:v where code=:c", s=s, v=v, c=code)
    sql("delete from regions where code like 'test-%'")
    region_svc.invalidate_cache()


def set_flags(**codes):
    """set_flags(us=(True, True), eu=(True, False)) -> scheduled, voting."""
    for code, (s, v) in codes.items():
        sql("update regions set scheduled=:s, voting=:v where code=:c", s=s, v=v, c=code.replace("_", "-"))
    region_svc.invalidate_cache()


def make_monitor(interval=300, status="UP"):
    uid, pid, mid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sql("insert into users (id,email,password_hash,plan,email_verified,created_at) "
        "values (:i,:e,'x','FREE',true,now())", i=uid, e=f"regions-{uid.hex[:10]}@eramirez.dev")
    sql("insert into projects (id,user_id,name,slug,created_at) values (:i,:u,'p',:s,now())",
        i=pid, u=uid, s=f"p-{uid.hex[:12]}")
    sql("insert into monitors (id,project_id,name,type,url,method,expected_status,interval_seconds,"
        "timeout_seconds,is_active,current_status,next_check_at,created_at) values "
        "(:i,:p,'m','HTTP','https://example.com','GET',200,:iv,10,true,:st,now() + interval '1 day',now())",
        i=mid, p=pid, iv=interval, st=status)
    return mid


def add_check(mid, region, status, age_minutes=0):
    sql("insert into checks (id,monitor_id,region,status,checked_at) "
        "values (:i,:m,:r,:s,now() - make_interval(mins => :a))",
        i=uuid.uuid4(), m=mid, r=region, s=status, a=age_minutes)


def status_of(mid):
    return sql("select current_status from monitors where id=:i", i=mid)[0][0]


def finalize(mid):
    return tasks.finalize_check_result.run(str(mid))


# ── migration seed ────────────────────────────────────────────────────────────

def test_seed_keeps_live_regions_and_adds_us_three_inactive():
    rows = {r[0]: (r[1], r[2]) for r in sql("select code, scheduled, voting from regions")}
    assert rows["us"] == rows["eu"] == rows["asia"] == (True, True)
    for code in ("us-east", "us-central", "us-west"):
        assert rows[code] == (False, False), code


def test_db_rejects_voting_without_scheduled_and_bad_codes(regions_snapshot):
    with pytest.raises(Exception, match="ck_regions_voting_needs_scheduled"):
        sql("update regions set scheduled=false, voting=true where code='us-west'")
    with pytest.raises(Exception, match="ck_regions_code_format"):
        sql("insert into regions (code,label) values ('US West!','x')")


def test_unknown_region_cannot_be_written():
    mid = make_monitor()
    with pytest.raises(Exception, match="fk_checks_region"):
        add_check(mid, "mars", "UP")


# ── the stale-vote bug (pre-CP-1: a dead worker's last UP voted forever) ────

def test_stale_up_votes_do_not_hold_a_dead_site_up(regions_snapshot):
    set_flags(us=(True, True), eu=(True, True), asia=(True, True))
    mid = make_monitor(interval=300, status="UP")
    # eu and asia workers died two hours ago, their last results were UP
    add_check(mid, "eu", "UP", age_minutes=120)
    add_check(mid, "asia", "UP", age_minutes=120)
    add_check(mid, "us", "DOWN", age_minutes=0)
    out = finalize(mid)
    # old code: 1 down of 3 -> UP forever. New: only 1 fresh vote, no quorum, no flip.
    assert out.get("skipped") == "no_quorum", out
    assert status_of(mid) == "UP"
    # once two fresh regions agree it goes DOWN, stale rows still ignored
    add_check(mid, "eu", "DOWN", age_minutes=0)
    out = finalize(mid)
    assert out["new_status"] == "down", out
    assert status_of(mid) == "DOWN"
    assert sql("select count(*) from incidents where monitor_id=:i and resolved_at is null", i=mid)[0][0] == 1


def test_fresh_window_scales_with_interval(regions_snapshot):
    set_flags(us=(True, True), eu=(True, True), asia=(True, True))
    mid = make_monitor(interval=3600, status="UP")       # hourly: 3h window
    add_check(mid, "us", "DOWN", age_minutes=100)
    add_check(mid, "eu", "DOWN", age_minutes=100)
    assert finalize(mid)["new_status"] == "down"


# ── shadow regions ────────────────────────────────────────────────────────────

def test_shadow_region_is_scheduled_but_never_votes(regions_snapshot, client):
    set_flags(us=(True, True), eu=(True, True), asia=(True, True),
              us_central=(True, False), us_west=(True, False))
    mid = make_monitor(status="UP")
    # scheduler enqueues all 5 scheduled regions
    sql("update monitors set next_check_at = now() - interval '1 second' where id=:i", i=mid)
    tasks.schedule_pending_checks.run()
    got = sorted(r[0] for r in sql("select region from pending_checks where monitor_id=:i", i=mid))
    assert got == ["asia", "eu", "us", "us-central", "us-west"]
    # the two shadows say DOWN, the voters say UP: monitor stays UP, no incident
    for code, st in (("us", "UP"), ("eu", "UP"), ("asia", "UP"), ("us-central", "DOWN"), ("us-west", "DOWN")):
        add_check(mid, code, st)
    assert finalize(mid)["new_status"] == "up"
    assert sql("select count(*) from incidents where monitor_id=:i", i=mid)[0][0] == 0


def test_unscheduled_region_gets_no_work_and_is_refused(regions_snapshot, client):
    set_flags(us_west=(False, False))
    hdr = {"X-Worker-Secret": SET.worker_secret, "X-Worker-Id": "test-west"}
    r = client.get("/internal/jobs?region=us-west", headers=hdr)
    assert r.status_code == 422 and "not a scheduled region" in r.json()["detail"], r.text
    r = client.post("/internal/heartbeat?region=us-west&version=1.1.0", headers=hdr)
    assert r.status_code == 422
    r = client.get("/internal/jobs?region=mars", headers=hdr)
    assert r.status_code == 422
    # flipping the flag takes effect without a deploy
    set_flags(us_west=(True, False))
    assert client.get("/internal/jobs?region=us-west", headers=hdr).status_code == 200
    assert client.post("/internal/heartbeat?region=us-west&version=1.1.0", headers=hdr).status_code == 200
    w = {x["region"]: x for x in client.get("/internal/workers", headers=hdr).json()["workers"]}
    assert w["us-west"]["scheduled"] is True and w["us-west"]["voting"] is False


def test_results_for_unknown_region_rejected_whole_batch(regions_snapshot, client):
    mid = make_monitor()
    hdr = {"X-Worker-Secret": SET.worker_secret, "X-Worker-Id": "test-x"}
    res = [{"pending_check_id": str(uuid.uuid4()), "monitor_id": str(mid), "region": "mars", "status": "up"}]
    r = client.post("/internal/results", headers=hdr, json={"results": res})
    assert r.status_code == 422 and "mars" in r.json()["detail"]
    assert sql("select count(*) from checks where monitor_id=:i", i=mid)[0][0] == 0


def test_old_worker_region_codes_still_work(client):
    """The three live 1.0.0 workers send region=us/eu/asia and must keep working unchanged."""
    hdr = {"X-Worker-Secret": SET.worker_secret, "X-Worker-Id": "test-legacy"}
    for code in ("us", "eu", "asia", "US", " eu "):
        assert client.get(f"/internal/jobs?region={code}", headers=hdr).status_code == 200, code


# ── the cutover itself, as row flips ─────────────────────────────────────────

def test_cutover_to_three_us_regions(regions_snapshot):
    """End state of CP-1: us-east/us-central/us-west vote, us/eu/asia retired."""
    set_flags(us=(False, False), eu=(False, False), asia=(False, False),
              us_east=(True, True), us_central=(True, True), us_west=(True, True))
    mid = make_monitor(status="UP")
    sql("update monitors set next_check_at = now() - interval '1 second' where id=:i", i=mid)
    tasks.schedule_pending_checks.run()
    got = sorted(r[0] for r in sql("select region from pending_checks where monitor_id=:i", i=mid))
    assert got == ["us-central", "us-east", "us-west"]
    # an old eu result (from before the flip) is not a voter any more
    add_check(mid, "eu", "DOWN")
    add_check(mid, "us-east", "DOWN")
    add_check(mid, "us-central", "UP")
    add_check(mid, "us-west", "UP")
    assert finalize(mid)["new_status"] == "up"
    add_check(mid, "us-west", "DOWN")
    assert finalize(mid)["new_status"] == "down"
