"""Durable streaming continuity and recovery status, isolated from replay."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stream_checkpoints",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("asset", sa.String(16), primary_key=True),
        sa.Column("last_trade_id", sa.BigInteger(), nullable=True),
        sa.Column("baseline_trade_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("stream_checkpoints")
