"""No-card Pro trial: pure rules (no database).

The database-backed flows (start, expiry, upgrade, webhook, migration) are in
tests/test_trial_db.py.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.models.models import PlanType
from app.services import plans, trial
from app.services.plans import PLAN_LIMITS, TRIAL_LIMITS, get_limits, trial_active

NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)
H = {"user-agent": "Mozilla/5.0 Chrome/128", "accept": "text/html"}


def user(plan=PlanType.FREE, ends=None, verified=True, used=False):
    return SimpleNamespace(plan=plan, trial_ends_at=ends, email_verified=verified, trial_used=used,
                           trial_offer=False, trial_reminder_sent_at=None, trial_expired_at=None)


def test_trial_limits_are_the_approved_numbers():
    assert (TRIAL_LIMITS.max_projects, TRIAL_LIMITS.max_monitors, TRIAL_LIMITS.min_interval_seconds) == (50, 100, 60)
    assert plans.TRIAL_DAYS == 14


def test_paid_plan_limits_and_prices_unchanged():
    assert PLAN_LIMITS[PlanType.FREE] == plans.PlanLimits(1, 3, 300)
    assert PLAN_LIMITS[PlanType.STARTER] == plans.PlanLimits(10, 40, 60)
    assert PLAN_LIMITS[PlanType.PRO] == plans.PlanLimits(50, 200, 30)


def test_get_limits_during_and_after_trial():
    u = user(ends=NOW + timedelta(days=3))
    assert trial_active(u, NOW) and get_limits(u, NOW) == TRIAL_LIMITS
    assert get_limits(u, NOW + timedelta(days=4)) == PLAN_LIMITS[PlanType.FREE]
    assert get_limits(user(), NOW) == PLAN_LIMITS[PlanType.FREE]


@pytest.mark.parametrize("plan", [PlanType.STARTER, PlanType.PRO])
def test_paid_plan_always_wins_over_a_trial(plan):
    u = user(plan=plan, ends=NOW + timedelta(days=10))
    assert not trial_active(u, NOW)
    assert get_limits(u, NOW) == PLAN_LIMITS[plan]


def test_start_rules():
    u = user()
    ends = trial.start(u, NOW)
    assert ends == NOW + timedelta(days=14) and u.trial_used and u.trial_ends_at == ends
    with pytest.raises(trial.TrialError, match="already used"):
        trial.start(u, NOW)
    with pytest.raises(trial.TrialError, match="Verify your email"):
        trial.start(user(verified=False), NOW)
    with pytest.raises(trial.TrialError, match="paid plan"):
        trial.start(user(plan=PlanType.STARTER), NOW)
    # an expired trial still counts as used
    with pytest.raises(trial.TrialError, match="already used"):
        trial.start(user(used=True, ends=NOW - timedelta(days=1)), NOW)


def test_days_left_rounds_up():
    assert trial.days_left(user(ends=NOW + timedelta(days=13, hours=1)), NOW) == 14
    assert trial.days_left(user(ends=NOW + timedelta(hours=2)), NOW) == 1
    assert trial.days_left(user(ends=NOW - timedelta(hours=2)), NOW) == 0


def _m(pid, active=True, reason=None):
    return trial.MonitorRow(uuid.uuid4(), pid, active, reason)


def test_plan_fit_keeps_oldest_project_and_its_oldest_monitors():
    p1, p2 = "p1", "p2"
    ms = [_m(p2), _m(p1), _m(p1), _m(p2), _m(p1), _m(p1)]   # oldest first, mixed projects
    run, pause = trial.plan_fit([p1, p2], ms, PLAN_LIMITS[PlanType.FREE])
    assert run == [ms[1].id, ms[2].id, ms[4].id]             # p1's three oldest
    assert set(pause) == {ms[0].id, ms[3].id, ms[5].id}


def test_plan_fit_leaves_user_paused_monitors_alone():
    p1 = "p1"
    user_paused = _m(p1, active=False, reason=None)
    ms = [user_paused, _m(p1), _m(p1, active=False, reason=trial.PAUSED_TRIAL_ENDED), _m(p1), _m(p1)]
    run, pause = trial.plan_fit([p1], ms, PLAN_LIMITS[PlanType.FREE])
    assert user_paused.id not in run and user_paused.id not in pause
    assert run == [ms[1].id, ms[2].id, ms[3].id]             # plan-paused one resumes in age order
    assert pause == [ms[4].id]


def test_plan_fit_respects_project_count_on_upgrade():
    pids = [f"p{i}" for i in range(12)]
    ms = [_m(p, active=False, reason=trial.PAUSED_TRIAL_ENDED) for p in pids for _ in range(5)]
    run, pause = trial.plan_fit(pids, ms, PLAN_LIMITS[PlanType.STARTER])   # 10 projects, 40 monitors
    assert len(run) == 40
    assert {m.project_id for m in ms if m.id in set(run)} <= set(pids[:10])
    assert len(pause) == 20


def test_reminder_email_is_honest_and_dash_free():
    u = SimpleNamespace(trial_ends_at=NOW + timedelta(days=3))
    subject, text, html = trial.reminder_text(u, 7, "https://checkpulse.dev")
    assert "3 days" in subject
    assert "7 of your monitors will be paused, not deleted" in text
    assert "https://checkpulse.dev/dashboard/billing" in text and "/dashboard/billing" in html
    assert "October 25" in text
    for s in (subject, text, html):
        assert "\u2014" not in s and "\u2013" not in s
    _, text0, _ = trial.reminder_text(u, 0, "https://checkpulse.dev")
    assert "keeps running" in text0


def test_reminder_goes_through_the_transactional_mailer_only():
    import inspect
    from app.workers import tasks
    src = inspect.getsource(tasks.trial_housekeeping)
    assert "mailer.send_sync" in src
    assert "crm" not in inspect.getsource(trial)


def test_beat_runs_housekeeping_hourly():
    from app.workers.celery_app import celery_app
    entry = celery_app.conf.beat_schedule["trial-housekeeping"]
    assert entry["task"] == "app.workers.tasks.trial_housekeeping"
    assert entry["schedule"].minute == {7} and len(entry["schedule"].hour) == 24


# ── public pages tell the truth about the trial ─────────────────────────────

def _text(path):
    r = TestClient(__import__("app.main", fromlist=["app"]).app).get(path, headers=H)
    assert r.status_code == 200
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))


def test_landing_pricing_mentions_trial_with_real_numbers():
    t = _text("/")
    L = TRIAL_LIMITS
    assert (f"try Pro free for {plans.TRIAL_DAYS} days, no card: {L.max_projects} sites, "
            f"{L.max_monitors} monitors, {L.min_interval_seconds // 60}-minute checks") in t
    # prices unchanged
    assert "$12" in t and "$49" in t


def test_getting_started_mentions_trial_with_real_numbers():
    t = _text("/docs/getting-started")
    L = TRIAL_LIMITS
    assert (f"try Pro free for {plans.TRIAL_DAYS} days, once, with no card: {L.max_projects} sites, "
            f"{L.max_monitors} monitors, {L.min_interval_seconds // 60}-minute checks") in t
    assert "paused, never deleted" in t
