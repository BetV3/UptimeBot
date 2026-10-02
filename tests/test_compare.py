"""Comparison pages: render, stay honest, and stay consistent with the product.

These tests pin the claims the page makes about CheckPulse to the code that
implements them, so a plan change cannot silently turn the page into a lie.
"""
import inspect
import re

from fastapi.testclient import TestClient

from app.api.routes import compare
from app.main import app
from app.models.models import PlanType
from app.services.plans import PLAN_LIMITS

H = {"user-agent": "Mozilla/5.0 Chrome/128", "accept": "text/html"}


def _page():
    r = TestClient(app).get("/vs/uptimerobot", headers=H)
    assert r.status_code == 200
    return r.text


def test_renders_with_seo_and_sources():
    html = _page()
    assert "CheckPulse vs UptimeRobot for agencies" in html
    assert '<link rel="canonical" href="https://checkpulse.dev/vs/uptimerobot">' in html
    # every UptimeRobot claim links to UptimeRobot's own pages
    links = re.findall(r'href="(https://[^"]+)"', html)
    vendor = [l for l in links if "uptimerobot.com" in l]
    assert len(vendor) >= 5
    assert all("rel=\"nofollow noopener\"" in html for _ in vendor)


def test_discloses_ownership_and_no_customers():
    html = _page()
    assert "CheckPulse is my product" in html
    assert "no agency customers yet" in html


def test_says_where_competitor_wins():
    html = _page()
    assert "Choose UptimeRobot if" in html
    assert "custom domains" in html.lower()


def test_no_unreliability_or_traction_claims():
    html = _page()
    article = html[html.index("<article"):html.index("</article>")]
    text = re.sub(r"<[^>]+>", " ", article).lower()
    for banned in ("unreliable", "cry wolf", "always down", "trusted by", "customers love", "thousands of", "\u2014"):
        assert banned not in text, banned


def test_never_claims_custom_domains_or_white_label_for_checkpulse():
    rows = {label: cp for label, _, cp in compare.UPTIMEROBOT_ROWS}
    assert rows["Custom domain on status page"] == "No"
    assert rows["White-label (no vendor footer)"] == "No"


def test_plan_numbers_match_plans_py():
    s, p, f = PLAN_LIMITS[PlanType.STARTER], PLAN_LIMITS[PlanType.PRO], PLAN_LIMITS[PlanType.FREE]
    html = _page()
    assert f"Starter ($12/month) covers {s.max_projects} projects and {s.max_monitors} monitors at {s.min_interval_seconds}-second" in html
    assert f"Pro ($49/month) covers {p.max_projects} projects and {p.max_monitors} monitors at {p.min_interval_seconds}-second" in html
    assert f"{f.max_projects} project and {f.max_monitors} monitors" in html
    assert f.min_interval_seconds == 300  # "5-minute checks" in the table


def test_two_of_three_claim_matches_scheduler():
    from app.workers import tasks
    src = inspect.getsource(tasks.finalize_check_result)
    assert "down_count >= 2" in src
    assert len(tasks.REGIONS) == 3


def test_facts_dated():
    assert compare.CHECKED in _page()
