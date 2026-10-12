import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import GUID, EncryptedString, UTCDateTime


class PortalCredential(Base):
    """The portal OAuth grant behind a local user: who they are on the portal
    and the tokens the agent presents to portal-mcp. One row per user; a new
    sign-in replaces it."""

    __tablename__ = "portal_credentials"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    portal_user_id: Mapped[str] = mapped_column(String(length=64), nullable=False)
    email: Mapped[str] = mapped_column(String(length=320), nullable=False)
    name: Mapped[str | None] = mapped_column(String(length=255), nullable=True)
    tier: Mapped[str] = mapped_column(String(length=16), nullable=False)
    issuer: Mapped[str] = mapped_column(String(length=255), nullable=False)
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    user = relationship("User", back_populates="portal_credential")
