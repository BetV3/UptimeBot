from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class CheckResult:
    status: str  # "up" | "down"
    response_time_ms: int | None = None
    status_code: int | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.response_time_ms is not None:
            payload["response_time_ms"] = self.response_time_ms
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.error is not None:
            payload["error"] = self.error
        payload.update(self.extra)
        return payload


class Checker(Protocol):
    def run(self, job: dict, client: Any) -> CheckResult:
        ...
