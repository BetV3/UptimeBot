import httpx
import pytest
from pytest_httpx import HTTPXMock

from worker.checkers import get_checker
from worker.checkers.http import HttpChecker


@pytest.fixture
def client():
    with httpx.Client() as c:
        yield c


def _job(**overrides) -> dict:
    base = {
        "pending_check_id": "pc-1",
        "monitor_id": "m-1",
        "type": "http",
        "url": "https://example.com/health",
        "method": "GET",
        "expected_status": 200,
        "timeout_seconds": 5,
        "headers": None,
        "body": None,
        "region": "us",
    }
    base.update(overrides)
    return base


def test_registry_returns_http_checker():
    checker = get_checker("http")
    assert isinstance(checker, HttpChecker)


def test_registry_unknown_type_raises():
    with pytest.raises(ValueError):
        get_checker("nope")


def test_up_when_status_matches(httpx_mock: HTTPXMock, client):
    httpx_mock.add_response(url="https://example.com/health", status_code=200)
    result = HttpChecker().run(_job(), client)
    assert result.status == "up"
    assert result.status_code == 200
    assert result.error is None
    assert result.response_time_ms is not None


def test_down_when_status_mismatches(httpx_mock: HTTPXMock, client):
    httpx_mock.add_response(url="https://example.com/health", status_code=500)
    result = HttpChecker().run(_job(), client)
    assert result.status == "down"
    assert result.status_code == 500
    assert "Expected 200, got 500" in result.error


def test_respects_expected_status(httpx_mock: HTTPXMock, client):
    httpx_mock.add_response(url="https://example.com/health", status_code=204)
    result = HttpChecker().run(_job(expected_status=204), client)
    assert result.status == "up"


def test_down_on_timeout(httpx_mock: HTTPXMock, client):
    httpx_mock.add_exception(httpx.ConnectTimeout("slow"))
    result = HttpChecker().run(_job(timeout_seconds=1), client)
    assert result.status == "down"
    assert "Timeout" in result.error


def test_down_on_network_error(httpx_mock: HTTPXMock, client):
    httpx_mock.add_exception(httpx.ConnectError("dns fail"))
    result = HttpChecker().run(_job(), client)
    assert result.status == "down"
    assert "dns fail" in result.error


def test_follows_redirects(httpx_mock: HTTPXMock, client):
    httpx_mock.add_response(
        url="https://example.com/health",
        status_code=302,
        headers={"Location": "https://example.com/final"},
    )
    httpx_mock.add_response(url="https://example.com/final", status_code=200)
    result = HttpChecker().run(_job(), client)
    assert result.status == "up"
    assert result.status_code == 200


def test_sends_headers_and_body(httpx_mock: HTTPXMock, client):
    httpx_mock.add_response(
        url="https://example.com/health",
        method="POST",
        status_code=200,
        match_headers={"X-Token": "abc"},
        match_content=b'{"ping":1}',
    )
    result = HttpChecker().run(
        _job(method="POST", headers={"X-Token": "abc"}, body='{"ping":1}'),
        client,
    )
    assert result.status == "up"


def test_payload_shape():
    from worker.checkers.base import CheckResult

    payload = CheckResult(status="up", response_time_ms=12, status_code=200).to_payload()
    assert payload == {"status": "up", "response_time_ms": 12, "status_code": 200}

    payload = CheckResult(status="down", error="boom").to_payload()
    assert payload == {"status": "down", "error": "boom"}
