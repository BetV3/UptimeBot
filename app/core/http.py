import json

from fastapi import Request

from app.core.config import get_settings


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "test", "testserver", "api"}


def _scope_header(scope: dict, name: str) -> str | None:
    header_name = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == header_name:
            return value.decode("latin-1")
    return None


def _forwarded_proto(scope: dict) -> str | None:
    proto = _scope_header(scope, "x-forwarded-proto") or _scope_header(scope, "x-forwarded-scheme")
    if proto:
        return proto.split(",", 1)[0].strip().lower()

    cf_visitor = _scope_header(scope, "cf-visitor")
    if cf_visitor:
        try:
            return json.loads(cf_visitor).get("scheme", "").lower() or None
        except json.JSONDecodeError:
            return None
    return None


def request_scheme(scope: dict) -> str:
    return _forwarded_proto(scope) or scope.get("scheme", "http")


def request_host(scope: dict) -> str | None:
    forwarded_host = _scope_header(scope, "x-forwarded-host")
    if forwarded_host:
        return forwarded_host.split(",", 1)[0].strip()
    return _scope_header(scope, "host")


def scope_is_secure(scope: dict) -> bool:
    return request_scheme(scope) == "https"


def request_is_secure(request: Request) -> bool:
    return scope_is_secure(request.scope)


def external_base_url(request: Request | None = None) -> str:
    if request is not None:
        host = request_host(request.scope)
        if host:
            hostname = host.split(":", 1)[0].lower()
            if hostname not in _LOCAL_HOSTS:
                return f"{request_scheme(request.scope)}://{host}".rstrip("/")

    return get_settings().app_url.rstrip("/")


def should_secure_cookie() -> bool:
    return get_settings().app_env == "production"
