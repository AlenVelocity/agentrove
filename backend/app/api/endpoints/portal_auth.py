from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.user_manager import current_active_user
from app.models.db_models.user import User
from app.models.schemas.portal_auth import (
    PortalAuthConfig,
    PortalIdentityOut,
    PortalLoginStart,
    PortalLoginStatus,
)
from app.services.exceptions import AuthException
from app.services.portal_auth import PortalAuthService, portal_auth_service

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def _require_enabled() -> None:
    if not PortalAuthService.enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Portal sign-in is not configured",
        )


def _page(title: str, body: str) -> HTMLResponse:
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:100vh;margin:0;background:#111;color:#eee}"
        "main{text-align:center;max-width:28rem;padding:2rem}h1{font-size:1.4rem}"
        "p{color:#aaa}</style></head><body><main>"
        f"<h1>{escape(title)}</h1><p>{escape(body)}</p></main></body></html>"
    )
    return HTMLResponse(html)


@router.get("/config", response_model=PortalAuthConfig)
async def portal_config() -> PortalAuthConfig:
    enabled = PortalAuthService.enabled()
    return PortalAuthConfig(
        enabled=enabled, issuer=PortalAuthService.issuer() if enabled else None
    )


@router.post("/start", response_model=PortalLoginStart)
@limiter.limit("10/minute")
async def start_portal_login(request: Request) -> PortalLoginStart:
    _require_enabled()
    try:
        state, authorize_url = await portal_auth_service.start()
    except AuthException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return PortalLoginStart(state=state, authorize_url=authorize_url)


@router.get("/callback", response_class=HTMLResponse)
async def portal_callback(
    state: str = Query(...),
    code: str | None = Query(default=None),
    iss: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> HTMLResponse:
    _require_enabled()
    if error or not code:
        portal_auth_service.fail(state, error or "access_denied", error_description)
        return _page(
            "Sign-in was not completed",
            error_description or "You can close this tab and try again in Agentrove.",
        )
    try:
        await portal_auth_service.complete(state, code, iss)
    except AuthException as e:
        return _page("Sign-in failed", e.message)
    return _page("You're signed in", "You can close this tab and return to Agentrove.")


@router.get("/poll", response_model=PortalLoginStatus)
@limiter.limit("120/minute")
async def poll_portal_login(
    request: Request, state: str = Query(...)
) -> PortalLoginStatus:
    _require_enabled()
    return PortalLoginStatus(**portal_auth_service.poll(state))


@router.get("/me", response_model=PortalIdentityOut)
async def portal_identity(
    user: User = Depends(current_active_user),
) -> PortalIdentityOut:
    credential = await portal_auth_service.get_credential(user.id)
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="This account is not signed in with the portal",
        )
    return PortalIdentityOut(
        email=credential.email,
        name=credential.name,
        tier=credential.tier,
        issuer=credential.issuer,
        expires_at=credential.expires_at,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def portal_logout(user: User = Depends(current_active_user)) -> None:
    await portal_auth_service.sign_out(user.id)
