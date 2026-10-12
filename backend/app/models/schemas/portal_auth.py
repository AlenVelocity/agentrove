from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class PortalAuthConfig(BaseModel):
    enabled: bool
    issuer: str | None = None


class PortalLoginStart(BaseModel):
    state: str
    authorize_url: str


class PortalLoginStatus(BaseModel):
    status: Literal["pending", "complete", "error"]
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"
    detail: str | None = None


class PortalIdentityOut(BaseModel):
    email: str
    name: str | None
    tier: str
    issuer: str
    expires_at: datetime
