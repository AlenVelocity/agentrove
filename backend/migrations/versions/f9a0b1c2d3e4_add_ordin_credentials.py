"""add ordin credentials

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

from alembic import op
import sqlalchemy as sa

from app.db.types import GUID, EncryptedString, UTCDateTime

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ordin_credentials",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("api_url", sa.String(255), nullable=False),
        sa.Column("access_token", EncryptedString(), nullable=False),
        sa.Column("refresh_token", EncryptedString(), nullable=False),
        sa.Column("access_expires_at", UTCDateTime(), nullable=False),
        sa.Column(
            "created_at", UTCDateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", UTCDateTime(), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("ordin_credentials")
