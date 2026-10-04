from pydantic import BaseModel, Field


class MonitorCreate(BaseModel):
    """REST create. `type` picks the check; each type reads its own fields.

    http: url, method, expected_status
    ssl:  target_host, target_port (443), warn_days_before_expiry (14)
    dns:  target_host, dns_record_type (A), dns_expected_value, dns_resolver,
          dns_match_mode (all|any)
    The same validators as the dashboard form run on every type.
    """
    name: str
    type: str = "http"
    url: str | None = None
    method: str = "GET"
    expected_status: int = 200
    interval_seconds: int = 300
    timeout_seconds: int = 10
    headers: dict | None = None
    body: str | None = None
    target_host: str | None = None
    target_port: int = 443
    warn_days_before_expiry: int | None = 14
    dns_record_type: str = "A"
    dns_expected_value: str | None = None
    dns_resolver: str | None = None
    dns_match_mode: str = "all"


class MonitorUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    method: str | None = None
    expected_status: int | None = None
    interval_seconds: int | None = None
    timeout_seconds: int | None = None
    headers: dict | None = None
    body: str | None = None


class MonitorResponse(BaseModel):
    id: str
    project_id: str
    name: str
    type: str = "http"
    url: str
    method: str
    expected_status: int
    interval_seconds: int
    timeout_seconds: int
    headers: dict | None = None
    body: str | None = None
    target_host: str | None = None
    target_port: int | None = None
    warn_days_before_expiry: int | None = None
    dns_record_type: str | None = None
    dns_expected_value: str | None = None
    is_active: bool
    current_status: str
    last_checked_at: str | None = None
    created_at: str

    model_config = {"from_attributes": True}


class LiveCheckRequest(BaseModel):
    """POST /monitors/{id}/check-now: ask all regions to check now and wait."""
    wait_seconds: int = Field(default=45, ge=5, le=90)
