"""Retain reproducible weighted-median candidate reports."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "aggregation_rounds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("weighting_report_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_aggregation_lookup", "aggregation_rounds", ["dataset", "cutoff", "id"])


def downgrade() -> None:
    op.drop_index("ix_aggregation_lookup", table_name="aggregation_rounds")
    op.drop_table("aggregation_rounds")
