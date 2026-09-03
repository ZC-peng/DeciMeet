"""Remove the retired experimental realtime meeting schema.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-09-03 12:00:00.000000+00:00

The historical revisions remain intact so existing development databases can
upgrade normally.  A fresh install reaches a schema that matches the public
application models after this terminal cleanup revision is applied.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop tables that were used only by the retired realtime/SFU prototype."""
    op.drop_table("rooms")
    op.drop_table("realtime_sessions")


def downgrade() -> None:
    """Restore the final historical shape of the retired realtime schema."""
    op.create_table(
        "realtime_sessions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("meeting_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "participants",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "segment_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_realtime_sessions_meeting_id",
        "realtime_sessions",
        ["meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_realtime_sessions_status",
        "realtime_sessions",
        ["status"],
        unique=False,
    )

    op.create_table(
        "rooms",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("meeting_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "scene",
            sa.String(length=50),
            server_default="generic",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="active",
            nullable=False,
        ),
        sa.Column("sfu_router_id", sa.String(length=100), nullable=True),
        sa.Column("participants", sa.ARRAY(sa.String()), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["meeting_id"],
            ["meetings.id"],
            name="rooms_meeting_id_fkey",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rooms_meeting_id",
        "rooms",
        ["meeting_id"],
        unique=False,
    )
    op.create_index(
        "ix_rooms_status",
        "rooms",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_rooms_scene",
        "rooms",
        ["scene"],
        unique=False,
    )
