from datetime import datetime

from pydantic import BaseModel, Field


class OrdinCloudConfig(BaseModel):
    enabled: bool
    web_url: str | None = None


class OrdinCloudStatus(BaseModel):
    connected: bool
    email: str | None = None
    # Where the frontend's cloud client should point, with local tokens minted
    # for it. Only returned by a connect; a restart keeps the refresh token.
    proxy_url: str | None = None
    proxy_access_token: str | None = None
    proxy_refresh_token: str | None = None


class OrdinConnectRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=256)


class OrdinCloudChat(BaseModel):
    id: str
    title: str
    case_id: str | None = None
    created_at: datetime
    updated_at: datetime
    pinned_at: datetime | None = None
    web_url: str


class OrdinCloudChats(BaseModel):
    items: list[OrdinCloudChat]
    page: int
    per_page: int
    total: int
    pages: int
