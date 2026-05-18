from .base import CheckResult, Checker
from .dns import DnsChecker
from .http import HttpChecker
from .ssl import SslChecker

_CHECKERS: dict[str, Checker] = {
    "http": HttpChecker(),
    "ssl": SslChecker(),
    "dns": DnsChecker(),
}


def get_checker(monitor_type: str) -> Checker:
    checker = _CHECKERS.get(monitor_type)
    if checker is None:
        raise ValueError(f"No checker registered for monitor type: {monitor_type}")
    return checker


__all__ = ["CheckResult", "Checker", "DnsChecker", "HttpChecker", "SslChecker", "get_checker"]
