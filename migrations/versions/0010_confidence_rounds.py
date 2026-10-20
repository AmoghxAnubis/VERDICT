"""Retain quality breakdowns and minimum availability gate decisions."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "confidence_rounds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aggregation_report_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_confidence_lookup", "confidence_rounds", ["dataset", "cutoff", "id"])


def downgrade() -> None:
    op.drop_index("ix_confidence_lookup", table_name="confidence_rounds")
    op.drop_table("confidence_rounds")
