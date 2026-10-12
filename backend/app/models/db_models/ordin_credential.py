import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base
from app.db.types import GUID, EncryptedString, UTCDateTime


class OrdinCredential(Base):
    """The Ordin (cloud) session behind a local user, so the app can list and
    open their cloud chats. One row per user; connecting again replaces it."""

    __tablename__ = "ordin_credentials"

    id: Mapped[UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    email: Mapped[str] = mapped_column(String(length=320), nullable=False)
    api_url: Mapped[str] = mapped_column(String(length=255), nullable=False)
    access_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    refresh_token: Mapped[str] = mapped_column(EncryptedString, nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)

    user = relationship("User", back_populates="ordin_credential")
