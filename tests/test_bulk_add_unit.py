"""Bulk add parsing and site-check verdicts: pure functions, no database."""
import pytest

from app.services import bulk_add, site_check


@pytest.mark.parametrize("text,urls", [
    ("a.com", ["https://a.com"]),
    ("a.com\nb.org\n\n  c.net  ", ["https://a.com", "https://b.org", "https://c.net"]),
    ("a.com, b.org; c.net d.io", ["https://a.com", "https://b.org", "https://c.net", "https://d.io"]),
    ("http://a.com", ["http://a.com"]),
    ("https://A.COM/Shop/", ["https://a.com/Shop/"]),
    ("https://a.com/", ["https://a.com"]),
    ("<https://a.com>", ["https://a.com"]),
    ("a.com.", ["https://a.com"]),
    ("https://a.com:8443/x", ["https://a.com:8443/x"]),
    ("https://a.com/x#frag", ["https://a.com/x"]),
])
def test_parse_normalizes(text, urls):
    assert [s.url for s in bulk_add.parse(text)] == urls


def test_parse_dedupes_keeping_order():
    got = [s.url for s in bulk_add.parse("b.com\na.com\nhttps://b.com\nB.COM\na.com/")]
    assert got == ["https://b.com", "https://a.com"]


def test_names_drop_www_and_keep_path():
    s = bulk_add.parse("https://www.client.com/shop/")[0]
    assert s.name == "client.com/shop" and s.host == "www.client.com"


@pytest.mark.parametrize("bad", ["localhost", "ftp://a.com", "justaword", "https://user:pw@a.com"])
def test_parse_reports_bad_entries(bad):
    (s,) = bulk_add.parse(bad)
    assert s.url is None and s.error


def test_parse_empty():
    assert bulk_add.parse("") == [] and bulk_add.parse(" \n ,, ") == []


def test_same_url_ignores_trailing_slash_and_case():
    assert bulk_add.same_url("https://A.com/", "https://a.com")
    assert not bulk_add.same_url("https://a.com/x", "https://a.com")


@pytest.mark.parametrize("check,kind", [
    (None, "pending"),
    ({"status": "up", "status_code": 200, "ms": 120}, "up"),
    ({"status": "up", "status_code": 200, "ms": 4200}, "slow"),
    ({"status": "down", "status_code": 403, "ms": 90}, "blocked"),
    ({"status": "down", "status_code": 429, "ms": 90}, "blocked"),
    ({"status": "down", "status_code": 503, "ms": 90}, "error"),
    ({"status": "down", "status_code": 404, "ms": 90}, "unexpected"),
    ({"status": "down", "status_code": None, "ms": None, "error": "Timeout after 8s"}, "down"),
])
def test_classify(check, kind):
    assert site_check.classify(check) == kind


@pytest.mark.parametrize("cells,verdict", [
    (["up", "up", "up"], "up"),
    (["up", "slow", "up"], "slow"),
    (["up", "blocked", "up"], "blocked from some locations"),
    (["blocked", "blocked", "blocked"], "blocked"),
    (["up", "down", "up"], "down from some locations"),
    (["down", "down", "error"], "down"),
    (["pending", "pending", "pending"], "no answer"),
])
def test_site_verdict(cells, verdict):
    assert site_check.site_verdict(cells) == verdict


@pytest.mark.parametrize("v,ok", [("1.1.0", True), ("1.2.3", True), ("2.0", True),
                                  ("1.0.0", False), ("", False), (None, False), ("dev", False)])
def test_worker_version_gate(v, ok):
    assert (site_check._version(v) >= site_check.MIN_WORKER_VERSION) is ok


def test_site_check_page_copy_has_no_em_dashes():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/templates/tool_site_check.html").read_text()
    assert "\u2014" not in src
