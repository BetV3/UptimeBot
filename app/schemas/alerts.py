from pydantic import BaseModel


class AlertChannelCreate(BaseModel):
    type: str  # "discord_webhook", "telegram", "email"
    config: dict  # e.g. {"webhook_url": "..."} or {"bot_token": "...", "chat_id": "..."}
    is_active: bool = True


class AlertChannelUpdate(BaseModel):
    config: dict | None = None
    is_active: bool | None = None


class AlertChannelResponse(BaseModel):
    id: str
    project_id: str
    type: str
    config: dict
    is_active: bool
    created_at: str

    model_config = {"from_attributes": True}
