from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.db_models.ordin_credential import OrdinCredential
from app.services.exceptions import AuthException

logger = logging.getLogger(__name__)
settings = get_settings()

HTTP_TIMEOUT_SECONDS = 20.0
# Ordin access tokens last 15 minutes; refresh this close to the end.
REFRESH_LEEWAY_SECONDS = 60
DEFAULT_ACCESS_LIFETIME_SECONDS = 15 * 60
SESSION_ENDED = "The Ordin session has ended; connect again"


@dataclass(frozen=True)
class OrdinTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jwt_expiry(token: str) -> datetime:
    # Read `exp` without verifying: only Ordin can verify its own tokens, and
    # we only need to know when to refresh.
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
    except (IndexError, KeyError, ValueError, TypeError):
        return _utc_now() + timedelta(seconds=DEFAULT_ACCESS_LIFETIME_SECONDS)


class OrdinCloudService:
    """Keeps an Ordin session per local user and calls Ordin on their behalf.

    Ordin runs the same fastapi-users login as this backend, but its refresh
    rotation is strict: a replayed refresh token revokes every session the
    user has, web included. So one refresh runs at a time, the rotated token
    is stored before anything else happens, and a refused refresh drops the
    connection instead of retrying.
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        self.session_factory = session_factory or SessionLocal
        self._refresh_lock = asyncio.Lock()

    @staticmethod
    def enabled() -> bool:
        return bool(settings.ORDIN_API_URL and settings.ORDIN_WEB_URL)

    @staticmethod
    def api_base() -> str:
        return f"{settings.ORDIN_API_URL.rstrip('/')}/api/v1"

    @staticmethod
    def chat_web_url(chat_id: str) -> str:
        return f"{settings.ORDIN_WEB_URL.rstrip('/')}/chat/{chat_id}"

    async def status(self, user_id: UUID) -> OrdinCredential | None:
        async with self.session_factory() as db:
            return await self._load(db, user_id)

    async def connect(self, user_id: UUID, email: str, password: str) -> str:
        """Signs in to Ordin with the user's own credentials. The password is
        forwarded once and never stored; only the tokens are."""
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.api_base()}/auth/jwt/login",
                    data={"username": email, "password": password},
                )
        except httpx.HTTPError as e:
            logger.error("Ordin login request failed: %s", e)
            raise AuthException("Could not reach Ordin", status_code=503) from e
        if response.status_code != 200:
            raise AuthException(ordin_error(response, "Ordin refused the sign-in"))
        tokens = _tokens_from(response.json())
        account_email = await self._fetch_email(tokens.access_token) or email.lower()
        async with self.session_factory() as db:
            credential = await self._load(db, user_id)
            if credential is None:
                credential = OrdinCredential(user_id=user_id)
                db.add(credential)
            credential.email = account_email
            credential.api_url = self.api_base()
            _apply_tokens(credential, tokens)
            await db.commit()
        return account_email

    async def disconnect(self, user_id: UUID) -> None:
        async with self.session_factory() as db:
            credential = await self._load(db, user_id)
            if credential is None:
                return
            refresh_token = credential.refresh_token
            await db.delete(credential)
            await db.commit()
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                await client.post(
                    f"{self.api_base()}/auth/jwt/logout",
                    json={"refresh_token": refresh_token},
                )
        except httpx.HTTPError as e:
            logger.warning("Could not log the Ordin session out for %s: %s", user_id, e)

    async def request(
        self,
        user_id: UUID,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        """One Ordin API call as the user. A 401 means the session is gone on
        Ordin's side, so the stored connection is dropped."""
        token = await self.access_token(user_id)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method,
                    f"{self.api_base()}{path}",
                    params=params,
                    data=data,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as e:
            logger.error("Ordin %s %s failed: %s", method, path, e)
            raise AuthException("Could not reach Ordin", status_code=503) from e
        if response.status_code == 401:
            await self.drop(user_id)
            raise AuthException(SESSION_ENDED)
        return response

    @asynccontextmanager
    async def stream(
        self, path: str, params: dict[str, Any]
    ) -> AsyncIterator[tuple[int, AsyncIterator[str]]]:
        """An open SSE response from Ordin, as (status code, line iterator).
        Ordin's stream route authenticates with its own `stream_token`, so no
        user token is sent."""
        timeout = httpx.Timeout(None, connect=HTTP_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET", f"{self.api_base()}{path}", params=params
            ) as response:
                yield response.status_code, response.aiter_lines()

    async def list_chats(
        self, user_id: UUID, page: int, per_page: int
    ) -> dict[str, Any]:
        response = await self.request(
            user_id, "GET", "/chat/chats", params={"page": page, "per_page": per_page}
        )
        if response.status_code != 200:
            raise AuthException(
                ordin_error(response, "Ordin could not list the chats"), status_code=502
            )
        body = response.json()
        return {
            "items": [
                {
                    "id": str(chat["id"]),
                    "title": chat.get("title") or "Untitled chat",
                    "case_id": chat.get("case_id"),
                    "created_at": chat["created_at"],
                    "updated_at": chat["updated_at"],
                    "pinned_at": chat.get("pinned_at"),
                    "web_url": self.chat_web_url(str(chat["id"])),
                }
                for chat in body.get("items", [])
            ],
            "page": body.get("page", page),
            "per_page": body.get("per_page", per_page),
            "total": body.get("total", 0),
            "pages": body.get("pages", 0),
        }

    async def access_token(self, user_id: UUID) -> str:
        async with self._refresh_lock:
            async with self.session_factory() as db:
                credential = await self._load(db, user_id)
                if credential is None:
                    raise AuthException("Ordin is not connected", status_code=404)
                leeway = timedelta(seconds=REFRESH_LEEWAY_SECONDS)
                if credential.access_expires_at - _utc_now() > leeway:
                    return credential.access_token
                try:
                    tokens = await self._refresh(credential.refresh_token)
                except AuthException as e:
                    logger.warning(
                        "Ordin session for %s is gone (%s); dropping it",
                        user_id,
                        e.message,
                    )
                    await db.delete(credential)
                    await db.commit()
                    raise AuthException(SESSION_ENDED) from e
                _apply_tokens(credential, tokens)
                await db.commit()
                return tokens.access_token

    async def drop(self, user_id: UUID) -> None:
        async with self.session_factory() as db:
            credential = await self._load(db, user_id)
            if credential is not None:
                await db.delete(credential)
                await db.commit()

    async def _refresh(self, refresh_token: str) -> OrdinTokens:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{self.api_base()}/auth/jwt/refresh",
                    json={"refresh_token": refresh_token},
                )
        except httpx.HTTPError as e:
            raise AuthException("Could not reach Ordin", status_code=503) from e
        if response.status_code != 200:
            raise AuthException(ordin_error(response, "Ordin refused the refresh"))
        return _tokens_from(response.json())

    async def _fetch_email(self, access_token: str) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{self.api_base()}/auth/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        email = response.json().get("email")
        return str(email).lower() if email else None

    @staticmethod
    async def _load(db: AsyncSession, user_id: UUID) -> OrdinCredential | None:
        result = await db.execute(
            select(OrdinCredential).where(OrdinCredential.user_id == user_id)
        )
        return result.scalar_one_or_none()


def _tokens_from(body: dict[str, Any]) -> OrdinTokens:
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not access_token or not refresh_token:
        raise AuthException("Ordin returned an incomplete token response")
    return OrdinTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=_jwt_expiry(access_token),
    )


def _apply_tokens(credential: OrdinCredential, tokens: OrdinTokens) -> None:
    credential.access_token = tokens.access_token
    credential.refresh_token = tokens.refresh_token
    credential.access_expires_at = tokens.access_expires_at


def ordin_error(response: httpx.Response, fallback: str) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, str) and detail:
        return detail
    return f"{fallback} (HTTP {response.status_code})"


ordin_cloud_service = OrdinCloudService()
