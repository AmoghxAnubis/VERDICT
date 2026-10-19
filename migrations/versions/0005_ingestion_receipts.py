"""Deduplicate Redis attempts atomically with evidence and health writes."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingestion_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ingestion_receipts")
