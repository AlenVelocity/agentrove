from collections.abc import Awaitable
from typing import Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.core.deps import get_refresh_token_service
from app.core.security import get_current_user
from app.core.user_manager import get_jwt_strategy
from app.db.session import get_db
from app.models.db_models.user import User
from app.models.schemas.auth import RefreshTokenRequest, Token
from app.services.exceptions import AuthException
from app.services.ordin_cloud import OrdinCloudService
from app.services.ordin_proxy import ORDIN_WORKSPACE_ID, ProxyError, ordin_proxy_service
from app.services.refresh_token import RefreshTokenService
from app.utils.parsing import parse_stream_cursors

"""The user's Ordin chats, served in the shape of a remote Agentrove instance
so the frontend's cloud mode can list, open, stream and continue them. Mounted
at /ordin-proxy/api/v1; the frontend points its cloud client there after the
Ordin connect and authenticates with local tokens minted for that purpose."""

router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _require_enabled() -> None:
    if not OrdinCloudService.enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ordin cloud is not configured",
        )


def _http(error: ProxyError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.detail)


T = TypeVar("T")


async def _call(coro: Awaitable[T]) -> T:
    try:
        return await coro
    except ProxyError as e:
        raise _http(e)
    except AuthException as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ---- auth: the cloud client refreshes its (local) tokens here ----------------


@router.post("/auth/jwt/refresh", response_model=Token)
async def refresh(
    request: Request,
    refresh_request: RefreshTokenRequest,
    db: AsyncSession = Depends(get_db),
    refresh_token_service: RefreshTokenService = Depends(get_refresh_token_service),
) -> Token:
    try:
        user, new_refresh_token = await refresh_token_service.validate_and_rotate(
            token=refresh_request.refresh_token,
            db=db,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except AuthException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    access_token = await get_jwt_strategy().write_token(user)
    return Token(
        access_token=access_token, refresh_token=new_refresh_token, token_type="bearer"
    )


# ---- chats ---------------------------------------------------------------------


@router.get("/chat/chats")
async def list_chats(
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_enabled()
    return await _call(ordin_proxy_service.list_chats(user.id, page, per_page))


@router.post("/chat/chats", status_code=status.HTTP_201_CREATED)
async def create_chat(
    body: dict[str, Any], user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _require_enabled()
    return await _call(
        ordin_proxy_service.create_chat(user.id, str(body.get("title") or ""))
    )


@router.get("/chat/chats/active-streams")
async def active_streams(
    user: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    _require_enabled()
    return await _call(ordin_proxy_service.active_streams(user.id))


@router.get("/chat/chats/events")
async def chat_events(user: User = Depends(get_current_user)) -> EventSourceResponse:
    _require_enabled()

    async def quiet() -> Any:
        # No lifecycle events from Ordin; the client polls the list anyway.
        await asyncio_forever()
        yield None  # pragma: no cover

    return EventSourceResponse(
        quiet(),
        headers=SSE_HEADERS,
        ping_message_factory=lambda: ServerSentEvent(event="ping", data=""),
    )


async def asyncio_forever() -> None:
    import asyncio

    await asyncio.Event().wait()


@router.get("/chat/chats/streams")
async def streams(
    cursors: str | None = Query(None, max_length=8192),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> EventSourceResponse:
    _require_enabled()
    try:
        requested = parse_stream_cursors(cursors)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        )
    await db.close()
    return EventSourceResponse(
        ordin_proxy_service.feed(
            user.id, {str(cid): seq for cid, seq in requested.items()}
        ),
        headers=SSE_HEADERS,
        ping_message_factory=lambda: ServerSentEvent(event="ping", data=""),
    )


@router.get("/chat/chats/{chat_id}")
async def get_chat(
    chat_id: UUID, user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _require_enabled()
    return await _call(ordin_proxy_service.get_chat(user.id, str(chat_id)))


@router.patch("/chat/chats/{chat_id}")
async def update_chat(
    chat_id: UUID, body: dict[str, Any], user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _require_enabled()
    return await _call(ordin_proxy_service.update_chat(user.id, str(chat_id), body))


@router.delete("/chat/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(chat_id: UUID, user: User = Depends(get_current_user)) -> None:
    _require_enabled()
    await _call(ordin_proxy_service.delete_chat(user.id, str(chat_id)))


@router.get("/chat/chats/{chat_id}/messages")
async def messages(
    chat_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_enabled()
    return await _call(
        ordin_proxy_service.messages(user.id, str(chat_id), limit, cursor)
    )


@router.get("/chat/chats/{chat_id}/status")
async def chat_status(
    chat_id: UUID, user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _require_enabled()
    return await _call(ordin_proxy_service.status(user.id, str(chat_id)))


@router.get("/chat/chats/{chat_id}/context-usage")
async def context_usage(
    chat_id: UUID, user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _require_enabled()
    return await _call(ordin_proxy_service.context_usage(user.id, str(chat_id)))


@router.post("/chat/chats/{chat_id}/viewed", status_code=status.HTTP_204_NO_CONTENT)
async def mark_viewed(chat_id: UUID, user: User = Depends(get_current_user)) -> None:
    _require_enabled()


@router.get("/chat/chats/{chat_id}/queue")
async def queue(
    chat_id: UUID, user: User = Depends(get_current_user)
) -> list[dict[str, Any]]:
    _require_enabled()
    return []


@router.get("/chat/chats/{chat_id}/sub-threads")
async def sub_threads(
    chat_id: UUID, user: User = Depends(get_current_user)
) -> list[dict[str, Any]]:
    _require_enabled()
    return []


@router.delete("/chat/chats/{chat_id}/stream", status_code=status.HTTP_204_NO_CONTENT)
async def stop_stream(chat_id: UUID, user: User = Depends(get_current_user)) -> None:
    _require_enabled()
    await _call(ordin_proxy_service.stop(user.id, str(chat_id)))


@router.post("/chat/chats/{chat_id}/permissions/{request_id}/respond")
async def respond_permission(
    chat_id: UUID,
    request_id: str,
    option_id: str = Form(...),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    _require_enabled()
    await _call(
        ordin_proxy_service.respond_permission(
            user.id, str(chat_id), request_id, option_id
        )
    )
    return {"success": True}


@router.post("/chat/chats/{chat_id}/elicitation/{request_id}/respond")
async def respond_elicitation(
    chat_id: UUID,
    request_id: str,
    body: dict[str, Any],
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    _require_enabled()
    content = body.get("content")
    await _call(
        ordin_proxy_service.respond_elicitation(
            user.id,
            str(chat_id),
            request_id,
            str(body.get("action") or "decline"),
            content if isinstance(content, dict) else None,
        )
    )
    return {"success": True}


@router.post("/chat/chat")
async def send(
    prompt: str = Form(...),
    chat_id: str = Form(...),
    model_id: str = Form(...),
    permission_mode: str | None = Form(None),
    thinking_mode: str | None = Form(None),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    # model_id is the local list's id; Ordin chats keep Ordin's own model.
    _require_enabled()
    return await _call(
        ordin_proxy_service.send(
            user.id, chat_id, prompt, permission_mode, thinking_mode
        )
    )


# ---- pseudo workspace and settings -----------------------------------------


@router.get("/workspaces")
async def workspaces(user: User = Depends(get_current_user)) -> dict[str, Any]:
    _require_enabled()
    return ordin_proxy_service.workspaces(user.id)


@router.get("/workspaces/{workspace_id}/resources")
async def workspace_resources(
    workspace_id: str, user: User = Depends(get_current_user)
) -> dict[str, Any]:
    _require_enabled()
    if workspace_id != ORDIN_WORKSPACE_ID:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown workspace"
        )
    return ordin_proxy_service.workspace_resources()


@router.get("/settings/")
@router.get("/settings")
async def settings(user: User = Depends(get_current_user)) -> dict[str, Any]:
    _require_enabled()
    return ordin_proxy_service.settings(user.id)


@router.get("/models/")
async def models() -> Response:
    # The frontend reads models from the local backend; nothing to serve here.
    return Response(status_code=status.HTTP_404_NOT_FOUND)
