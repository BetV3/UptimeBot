from pydantic import BaseModel


class MonitorCreate(BaseModel):
    name: str
    url: str
    method: str = "GET"
    expected_status: int = 200
    interval_seconds: int = 60
    timeout_seconds: int = 10
    headers: dict | None = None
    body: str | None = None


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
    url: str
    method: str
    expected_status: int
    interval_seconds: int
    timeout_seconds: int
    headers: dict | None = None
    body: str | None = None
    is_active: bool
    current_status: str
    created_at: str

    model_config = {"from_attributes": True}
