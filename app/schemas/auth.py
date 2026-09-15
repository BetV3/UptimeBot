from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserRegister(UserCreate):
    """Registration payload for the JSON API.

    Carries the Turnstile widget response so the JSON endpoint is gated
    exactly like the HTML form. Gating only the form would be pointless —
    the abusive client can just POST JSON instead. Aliased to the header-ish
    name Cloudflare's widget uses so the same field name works from a form
    body or a JSON client.
    """

    turnstile_token: str = Field(default="", alias="cf-turnstile-response")

    model_config = {"populate_by_name": True}


class UserResponse(BaseModel):
    id: str
    email: str
    plan: str
    created_at: str

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenRefresh(BaseModel):
    refresh_token: str
