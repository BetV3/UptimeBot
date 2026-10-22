"""Comparison pages (/vs/<competitor>).

Every competitor fact on these pages must come from the vendor's own docs and
be listed in growth/competitor_facts.md with a quote and access date. Never
claim a competitor is unreliable. Say where the competitor is the better
choice; credibility is the whole point of the page.

``CHECKED`` is the date the facts were last verified. Re-verify and bump it
when prices or behaviour may have changed (at least every quarter).
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

CHECKED = "Oct 2, 2026"

UPTIMEROBOT_ROWS = [
    ("Down when", "any single region fails 3 retries", "2 of 3 regions (US, EU, Asia) agree"),
    ("Check regions", "North America, Europe, Asia, Oceania", "US, EU, Asia"),
    ("50 sites at 60s or faster", "Solo 50: $28/mo, 60s", "Pro: $49/mo, 200 monitors, 30s"),
    ("Branded status pages", "3 on Solo, 100 on Team ($46/mo)", "1 per project: 10 on Starter, 50 on Pro"),
    ("Custom domain on status page", "Yes, Solo and up", "No"),
    ("White-label (no vendor footer)", "Team and up", "No"),
    ("SSL expiry and DNS checks", "Solo and up (not on Free)", "Every plan, including Free"),
    ("Free plan", "Yes, 5-minute checks, basic status page", "Yes, 1 project, 3 monitors, 5-minute checks"),
]


def _templates():
    from app.main import templates
    return templates


@router.get("/vs/uptimerobot", response_class=HTMLResponse)
async def vs_uptimerobot(request: Request):
    return _templates().TemplateResponse(
        "vs_uptimerobot.html",
        {"request": request, "rows": UPTIMEROBOT_ROWS, "checked": CHECKED},
    )
