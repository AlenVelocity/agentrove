from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import get_password_hash
from app.core.user_manager import get_jwt_strategy
from app.db.session import SessionLocal
from app.models.db_models.portal_credential import PortalCredential
from app.models.db_models.user import User, UserSettings
from app.services.exceptions import AuthException
from app.services.refresh_token import RefreshTokenService

logger = logging.getLogger(__name__)
settings = get_settings()

# A sign-in the user never finished in the browser is forgotten after this.
PENDING_LOGIN_TTL_SECONDS = 600
# Refresh a portal token this long before it expires, so an agent session that
# starts now still has a token that outlives its first tool calls.
REFRESH_LEEWAY_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 20.0
CALLBACK_PATH = "/auth/portal/callback"
MCP_SERVER_NAME = "portal"

_USERNAME_CHARS = re.compile(r"[^a-z0-9_-]+")


@dataclass
class PendingLogin:
    code_verifier: str
    started_at: float
    result: dict[str, str] | None = None
    error: str | None = None


@dataclass(frozen=True)
class PortalTokens:
    access_token: str
    refresh_token: str
    expires_at: datetime


@dataclass(frozen=True)
class PortalIdentity:
    portal_user_id: str
    email: str
    name: str | None
    tier: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PortalAuthService:
    """Signs the desktop user in with the portal's OAuth server (authorization
    code + PKCE, loopback redirect on this backend's own port) and keeps the
    resulting portal-mcp token fresh for the agent.

    Pending sign-ins live in memory: the desktop sidecar is one uvicorn worker,
    and a state value is only ever polled by the window that started it.
    """

    def __init__(self, session_factory: Any | None = None) -> None:
        self.session_factory = session_factory or SessionLocal
        self._pending: dict[str, PendingLogin] = {}
        self._metadata: dict[str, Any] | None = None
        self._refresh_lock = asyncio.Lock()

    @staticmethod
    def enabled() -> bool:
        return bool(
            settings.PORTAL_OAUTH_ISSUER
            and settings.PORTAL_OAUTH_CLIENT_ID
            and settings.PORTAL_MCP_URL
        )

    @staticmethod
    def issuer() -> str:
        return settings.PORTAL_OAUTH_ISSUER.rstrip("/")

    @staticmethod
    def redirect_uri() -> str:
        return f"{settings.BASE_URL.rstrip('/')}{settings.API_V1_STR}{CALLBACK_PATH}"

    async def start(self) -> tuple[str, str]:
        self._forget_stale_logins()
        metadata = await self._discover()
        code_verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(32)
        self._pending[state] = PendingLogin(
            code_verifier=code_verifier, started_at=time.monotonic()
        )
        params = {
            "response_type": "code",
            "client_id": settings.PORTAL_OAUTH_CLIENT_ID,
            "redirect_uri": self.redirect_uri(),
            "scope": "mcp",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": settings.PORTAL_MCP_URL,
        }
        return state, f"{metadata['authorization_endpoint']}?{urlencode(params)}"

    async def complete(self, state: str, code: str, issuer: str | None) -> None:
        pending = self._pending.get(state)
        if pending is None or pending.result or pending.error:
            raise AuthException("This sign-in attempt is unknown or has expired")
        metadata = await self._discover()
        # RFC 9207: the response names its issuer; refuse one from anywhere else.
        if issuer and issuer.rstrip("/") != str(metadata["issuer"]).rstrip("/"):
            pending.error = "The sign-in response came from an unexpected issuer"
            raise AuthException(pending.error)
        try:
            tokens = await self._exchange_code(metadata, code, pending.code_verifier)
            identity = await self._fetch_identity(tokens.access_token)
            pending.result = await self._establish_local_session(identity, tokens)
        except AuthException as e:
            pending.error = e.message
            raise
        except httpx.HTTPError as e:
            logger.error("Portal sign-in failed talking to the portal: %s", e)
            pending.error = "Could not reach the portal to finish signing in"
            raise AuthException(pending.error) from e

    def fail(self, state: str, error: str, description: str | None) -> None:
        pending = self._pending.get(state)
        if pending is None:
            return
        pending.error = description or error

    def poll(self, state: str) -> dict[str, Any]:
        self._forget_stale_logins()
        pending = self._pending.get(state)
        if pending is None:
            return {"status": "error", "detail": "This sign-in attempt has expired"}
        if pending.result:
            del self._pending[state]
            return {"status": "complete", **pending.result}
        if pending.error:
            del self._pending[state]
            return {"status": "error", "detail": pending.error}
        return {"status": "pending"}

    async def get_credential(self, user_id: UUID) -> PortalCredential | None:
        async with self.session_factory() as db:
            return await self._load_credential(db, user_id)

    async def get_access_token(self, user_id: UUID) -> str | None:
        """A portal-mcp token for this user, refreshed when close to expiry.
        None when the user never signed in with the portal or the grant is gone
        (revoked, or the refresh token replayed), in which case they sign in
        again and the agent simply runs without the portal tools."""
        async with self._refresh_lock:
            async with self.session_factory() as db:
                credential = await self._load_credential(db, user_id)
                if credential is None:
                    return None
                leeway = timedelta(seconds=REFRESH_LEEWAY_SECONDS)
                if credential.expires_at - _utc_now() > leeway:
                    return credential.access_token
                try:
                    tokens = await self._refresh_tokens(credential.refresh_token)
                except AuthException as e:
                    logger.warning(
                        "Portal grant for user %s is gone (%s); dropping it",
                        user_id,
                        e.message,
                    )
                    await db.delete(credential)
                    await db.commit()
                    return None
                credential.access_token = tokens.access_token
                credential.refresh_token = tokens.refresh_token
                credential.expires_at = tokens.expires_at
                await db.commit()
                return tokens.access_token

    async def mcp_server_config(self, user_id: UUID) -> dict[str, Any] | None:
        if not self.enabled():
            return None
        token = await self.get_access_token(user_id)
        if token is None:
            return None
        return {
            "name": MCP_SERVER_NAME,
            "type": "http",
            "url": settings.PORTAL_MCP_URL,
            "headers": {"Authorization": f"Bearer {token}"},
        }

    async def sign_out(self, user_id: UUID) -> None:
        async with self.session_factory() as db:
            credential = await self._load_credential(db, user_id)
            if credential is None:
                return
            refresh_token = credential.refresh_token
            await db.delete(credential)
            await db.commit()
        try:
            metadata = await self._discover()
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                await client.post(
                    metadata["revocation_endpoint"],
                    data={
                        "token": refresh_token,
                        "token_type_hint": "refresh_token",
                        "client_id": settings.PORTAL_OAUTH_CLIENT_ID,
                    },
                )
        except (httpx.HTTPError, AuthException) as e:
            # The local copy is gone either way; the grant expires on its own.
            logger.warning("Could not revoke the portal grant for %s: %s", user_id, e)

    async def _discover(self) -> dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        url = f"{self.issuer()}/.well-known/oauth-authorization-server"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
                response = await client.get(url)
                response.raise_for_status()
                metadata: dict[str, Any] = response.json()
        except httpx.HTTPError as e:
            logger.error("Portal OAuth discovery failed at %s: %s", url, e)
            raise AuthException(
                "Could not reach the portal's sign-in service", status_code=503
            ) from e
        for key in ("issuer", "authorization_endpoint", "token_endpoint"):
            if not metadata.get(key):
                raise AuthException(
                    f"The portal's OAuth metadata has no {key}", status_code=503
                )
        metadata.setdefault("revocation_endpoint", f"{self.issuer()}/api/oauth/revoke")
        self._metadata = metadata
        return metadata

    async def _exchange_code(
        self, metadata: dict[str, Any], code: str, code_verifier: str
    ) -> PortalTokens:
        return await self._token_request(
            metadata,
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "client_id": settings.PORTAL_OAUTH_CLIENT_ID,
                "redirect_uri": self.redirect_uri(),
                "resource": settings.PORTAL_MCP_URL,
            },
        )

    async def _refresh_tokens(self, refresh_token: str) -> PortalTokens:
        metadata = await self._discover()
        return await self._token_request(
            metadata,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": settings.PORTAL_OAUTH_CLIENT_ID,
            },
        )

    async def _token_request(
        self, metadata: dict[str, Any], form: dict[str, str]
    ) -> PortalTokens:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(metadata["token_endpoint"], data=form)
        if response.status_code != 200:
            raise AuthException(_oauth_error(response))
        body = response.json()
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        if not access_token or not refresh_token:
            raise AuthException("The portal returned an incomplete token response")
        expires_in = int(body.get("expires_in") or 3600)
        return PortalTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_utc_now() + timedelta(seconds=expires_in),
        )

    async def _fetch_identity(self, access_token: str) -> PortalIdentity:
        url = f"{self.issuer()}/api/oauth/userinfo"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                url, headers={"Authorization": f"Bearer {access_token}"}
            )
        if response.status_code != 200:
            raise AuthException(_oauth_error(response))
        body = response.json()
        email = str(body.get("email") or "").strip().lower()
        sub = str(body.get("sub") or "")
        if not email or not sub:
            raise AuthException("The portal did not say who signed in")
        name = body.get("name")
        return PortalIdentity(
            portal_user_id=sub,
            email=email,
            name=str(name) if name else None,
            tier=str(body.get("tier") or "client"),
        )

    async def _establish_local_session(
        self, identity: PortalIdentity, tokens: PortalTokens
    ) -> dict[str, str]:
        async with self.session_factory() as db:
            user = await self._get_or_create_user(db, identity)
            await self._store_credential(db, user.id, identity, tokens)
            access_token = await get_jwt_strategy().write_token(user)
            refresh_token = await RefreshTokenService(
                session_factory=self.session_factory
            ).create_refresh_token(
                user_id=user.id, db=db, user_agent="portal-sign-in", ip_address=None
            )
        return {"access_token": access_token, "refresh_token": refresh_token}

    @staticmethod
    async def _load_credential(
        db: AsyncSession, user_id: UUID
    ) -> PortalCredential | None:
        result = await db.execute(
            select(PortalCredential).where(PortalCredential.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def _get_or_create_user(
        self, db: AsyncSession, identity: PortalIdentity
    ) -> User:
        result = await db.execute(
            select(User).where(func.lower(User.email) == identity.email)
        )
        user = result.scalar_one_or_none()
        if user is not None:
            if not user.is_active:
                raise AuthException("This account is inactive", status_code=403)
            if not user.is_verified:
                # The portal vouched for the address.
                user.is_verified = True
                await db.commit()
            return user

        # A local row only; nobody signs in to it with a password.
        for attempt in range(3):
            user = User(
                email=identity.email,
                hashed_password=get_password_hash(secrets.token_urlsafe(32)),
                is_active=True,
                is_superuser=False,
                is_verified=True,
                username=self._username_for(identity.email, attempt),
            )
            db.add(user)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                continue
            db.add(UserSettings(user_id=user.id, github_personal_access_token=None))
            await db.commit()
            logger.info(
                "Created local user %s for portal account %s", user.id, identity.email
            )
            return user
        raise AuthException("Could not create a local account for this portal user")

    @staticmethod
    def _username_for(email: str, attempt: int) -> str:
        base = _USERNAME_CHARS.sub("-", email.split("@", 1)[0].lower()).strip("-")[:48]
        base = base or "user"
        if attempt == 0:
            return base
        return f"{base}-{secrets.token_hex(3)}"

    @staticmethod
    async def _store_credential(
        db: AsyncSession, user_id: UUID, identity: PortalIdentity, tokens: PortalTokens
    ) -> None:
        credential = await PortalAuthService._load_credential(db, user_id)
        if credential is None:
            credential = PortalCredential(user_id=user_id)
            db.add(credential)
        credential.portal_user_id = identity.portal_user_id
        credential.email = identity.email
        credential.name = identity.name
        credential.tier = identity.tier
        credential.issuer = PortalAuthService.issuer()
        credential.access_token = tokens.access_token
        credential.refresh_token = tokens.refresh_token
        credential.expires_at = tokens.expires_at
        await db.commit()

    def _forget_stale_logins(self) -> None:
        cutoff = time.monotonic() - PENDING_LOGIN_TTL_SECONDS
        for state in [s for s, p in self._pending.items() if p.started_at < cutoff]:
            del self._pending[state]


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    description = body.get("error_description") or body.get("error")
    if description:
        return f"The portal refused the request: {description}"
    return f"The portal refused the request (HTTP {response.status_code})"


portal_auth_service = PortalAuthService()
