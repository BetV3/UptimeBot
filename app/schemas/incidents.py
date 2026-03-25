from pydantic import BaseModel


class IncidentResponse(BaseModel):
    id: str
    monitor_id: str
    monitor_name: str
    started_at: str
    resolved_at: str | None = None
    duration_seconds: int | None = None

    model_config = {"from_attributes": True}
