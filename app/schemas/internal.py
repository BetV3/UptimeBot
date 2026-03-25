from pydantic import BaseModel


class CheckResult(BaseModel):
    pending_check_id: str
    monitor_id: str
    region: str
    status: str  # "up" or "down"
    status_code: int | None = None
    response_time_ms: int | None = None
    error: str | None = None


class CheckResultsBatch(BaseModel):
    results: list[CheckResult]
