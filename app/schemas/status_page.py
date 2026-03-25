from pydantic import BaseModel


class StatusPageUpdate(BaseModel):
    is_public: bool | None = None
    display_name: str | None = None
    logo_url: str | None = None
    primary_color: str | None = None


class StatusPageResponse(BaseModel):
    id: str
    project_id: str
    is_public: bool
    display_name: str
    logo_url: str | None = None
    primary_color: str

    model_config = {"from_attributes": True}
