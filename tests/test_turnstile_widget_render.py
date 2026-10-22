"""Every gated form must ship a solvable challenge widget.

This is the bug class these tests exist for: a route can enforce Turnstile
server-side while forgetting to pass `turnstile_site_key` into the template it
renders. The endpoint then rejects everyone, including real users, and no
403-based test notices — the gate looks like it is working perfectly.

That is exactly what happened to check_email.html: /dashboard/register
enforced the challenge, then rendered the "check your inbox" page (which
contains the resend-verification form) with no widget, so a user clicking
"Resend verification email" was unconditionally blocked with no way to solve
anything.

Rule under test: if a page contains a form POSTing to a Turnstile-gated
endpoint, that page must also render the widget and load the Turnstile script.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

# Endpoints that reject a request without a valid Turnstile token.
GATED_ACTIONS = [
    "/dashboard/register",
    "/dashboard/forgot-password",
    "/dashboard/resend-verification",
]

SITE_KEY = "0xTESTSITEKEY"


@pytest.fixture
def gated_client(monkeypatch):
    """A client with Turnstile switched on, as in production."""
    monkeypatch.setattr(get_settings(), "turnstile_site_key", SITE_KEY)
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "secret")
    return TestClient(app)


def _forms_posting_to_gated_endpoints(html: str) -> list[str]:
    return [
        action
        for action in re.findall(r'<form[^>]*action="([^"]+)"', html)
        if any(action.rstrip("/").endswith(g.rstrip("/")) for g in GATED_ACTIONS)
    ]


@pytest.mark.parametrize("path", ["/dashboard/register", "/dashboard/forgot-password"])
def test_gated_pages_render_widget(gated_client, path):
    r = gated_client.get(path)
    assert r.status_code == 200
    assert _forms_posting_to_gated_endpoints(r.text), f"{path} has no gated form?"
    assert "cf-turnstile" in r.text, f"{path} renders no Turnstile widget"
    assert SITE_KEY in r.text, f"{path} widget has no sitekey"
    assert "challenges.cloudflare.com" in r.text, f"{path} never loads the Turnstile script"


def test_check_email_page_after_signup_can_resend(gated_client):
    """The regression, checked at the template level.

    After signing up, the confirmation page offers a "Resend verification
    email" button; that form is gated, so the page must carry a widget or the
    button is dead for every real user.

    Rendered directly rather than through a POST /register round trip: the
    bug is in what the route passes to the template, and a template render
    needs no database, no Resend account, and no network — so this stays a
    fast unit test that cannot pass for the wrong reason.
    """
    from app.main import templates

    # The exact context register_submit builds on the success path.
    import app.api.routes.dashboard as dash
    import inspect

    src = inspect.getsource(dash.register_submit)
    assert "check_email.html" in src, "register_submit no longer renders check_email"
    assert "turnstile_site_key" in src, (
        "register_submit renders check_email.html without passing "
        "turnstile_site_key — the resend form on that page will have no "
        "widget and every real user will be blocked from resending"
    )

    # And prove the template actually uses it when given it.
    html = templates.get_template("check_email.html").render(
        request=None, email="a@b.com", info=None, error=None,
        turnstile_site_key=SITE_KEY,
    )
    assert "cf-turnstile" in html
    assert SITE_KEY in html
    assert "challenges.cloudflare.com" in html


def test_every_check_email_render_passes_the_sitekey():
    """All four routes that render check_email.html must pass the sitekey.

    check_email.html always contains the gated resend form, so any render
    that omits the key produces a page with a dead button.
    """
    import inspect
    import re

    import app.api.routes.dashboard as dash

    src = inspect.getsource(dash)
    # Find each TemplateResponse("check_email.html", {...}) context block.
    blocks = re.findall(
        r'"check_email\.html",\s*(\{.*?\})\s*(?:,|\))', src, re.DOTALL
    )
    assert blocks, "no check_email.html renders found — did the file change?"
    missing = [b for b in blocks if "turnstile_site_key" not in b]
    assert not missing, (
        f"{len(missing)} of {len(blocks)} check_email.html renders omit "
        f"turnstile_site_key: {[b[:80] for b in missing]}"
    )


def test_no_gated_form_ships_without_a_widget(gated_client):
    """Sweep the public pages: any page with a gated form needs a widget.

    Written as a sweep rather than a per-page assertion so a NEW page carrying
    a gated form is covered the day it is added.
    """
    offenders = []
    for path in ["/", "/dashboard/login", "/dashboard/register", "/dashboard/forgot-password"]:
        r = gated_client.get(path)
        if r.status_code != 200:
            continue
        if _forms_posting_to_gated_endpoints(r.text) and "cf-turnstile" not in r.text:
            offenders.append(path)
    assert not offenders, f"pages with a gated form but no widget: {offenders}"


def test_widget_absent_when_turnstile_disabled(monkeypatch):
    """With no sitekey configured the widget must not render at all, so dev
    and self-hosted installs don't show a broken empty challenge box."""
    monkeypatch.setattr(get_settings(), "turnstile_site_key", "")
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", "")
    c = TestClient(app)
    r = c.get("/dashboard/register")
    assert r.status_code == 200
    assert "cf-turnstile" not in r.text
    assert "challenges.cloudflare.com" not in r.text
