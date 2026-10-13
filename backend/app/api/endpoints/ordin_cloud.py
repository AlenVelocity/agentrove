from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.user_manager import current_active_user, get_jwt_strategy
from app.db.session import SessionLocal
from app.models.db_models.user import User
from app.models.schemas.ordin_cloud import (
    OrdinCloudChats,
    OrdinCloudConfig,
    OrdinCloudStatus,
    OrdinConnectRequest,
)
from app.services.exceptions import AuthException
from app.services.ordin_cloud import OrdinCloudService, ordin_cloud_service
from app.services.refresh_token import RefreshTokenService

settings = get_settings()
PROXY_PREFIX = "/ordin-proxy"
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def _require_enabled() -> None:
    if not OrdinCloudService.enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ordin cloud is not configured",
        )


@router.get("/config", response_model=OrdinCloudConfig)
async def ordin_config() -> OrdinCloudConfig:
    enabled = OrdinCloudService.enabled()
    return OrdinCloudConfig(
        enabled=enabled,
        web_url=settings.ORDIN_WEB_URL.rstrip("/") if enabled else None,
    )


@router.get("/status", response_model=OrdinCloudStatus)
async def ordin_status(user: User = Depends(current_active_user)) -> OrdinCloudStatus:
    _require_enabled()
    credential = await ordin_cloud_service.status(user.id)
    return OrdinCloudStatus(
        connected=credential is not None,
        email=credential.email if credential else None,
    )


@router.post("/connect", response_model=OrdinCloudStatus)
@limiter.limit("5/minute")
async def ordin_connect(
    request: Request,
    body: OrdinConnectRequest,
    user: User = Depends(current_active_user),
) -> OrdinCloudStatus:
    _require_enabled()
    try:
        email = await ordin_cloud_service.connect(user.id, body.email, body.password)
    except AuthException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return await _proxy_session(user, email)


@router.post("/proxy-session", response_model=OrdinCloudStatus)
async def ordin_proxy_session(
    user: User = Depends(current_active_user),
) -> OrdinCloudStatus:
    """Fresh proxy tokens for a connection made earlier (the app lost its cloud
    session, or connected before the proxy existed)."""
    _require_enabled()
    credential = await ordin_cloud_service.status(user.id)
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ordin is not connected"
        )
    return await _proxy_session(user, credential.email)


async def _proxy_session(user: User, email: str) -> OrdinCloudStatus:
    # The frontend's cloud client gets its own local session for the proxy, so
    # its refresh rotation never races the main session's.
    access_token = await get_jwt_strategy().write_token(user)
    async with SessionLocal() as db:
        refresh_token = await RefreshTokenService(
            session_factory=SessionLocal
        ).create_refresh_token(
            user_id=user.id, db=db, user_agent="ordin-proxy", ip_address=None
        )
    return OrdinCloudStatus(
        connected=True,
        email=email,
        proxy_url=f"{settings.BASE_URL.rstrip('/')}{PROXY_PREFIX}",
        proxy_access_token=access_token,
        proxy_refresh_token=refresh_token,
    )


@router.delete("/connect", status_code=status.HTTP_204_NO_CONTENT)
async def ordin_disconnect(user: User = Depends(current_active_user)) -> None:
    _require_enabled()
    await ordin_cloud_service.disconnect(user.id)


@router.get("/chats", response_model=OrdinCloudChats)
async def ordin_chats(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
    user: User = Depends(current_active_user),
) -> OrdinCloudChats:
    _require_enabled()
    try:
        chats = await ordin_cloud_service.list_chats(user.id, page, per_page)
    except AuthException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return OrdinCloudChats(**chats)
