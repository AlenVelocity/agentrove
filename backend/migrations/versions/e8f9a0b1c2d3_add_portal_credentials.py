"""add portal credentials

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""

from alembic import op
import sqlalchemy as sa

from app.db.types import GUID, EncryptedString, UTCDateTime

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_credentials",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("portal_user_id", sa.String(64), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("issuer", sa.String(255), nullable=False),
        sa.Column("access_token", EncryptedString(), nullable=False),
        sa.Column("refresh_token", EncryptedString(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column(
            "created_at", UTCDateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", UTCDateTime(), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("portal_credentials")
