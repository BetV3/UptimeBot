from pydantic import BaseModel


class ProjectCreate(BaseModel):
    name: str


class ProjectUpdate(BaseModel):
    name: str | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    slug: str
    created_at: str

    model_config = {"from_attributes": True}
