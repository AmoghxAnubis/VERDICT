"""Serialize immutable quarantine/weighting transitions per dataset."""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "weighting_rounds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scoring_report_id", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.UniqueConstraint("dataset", "cutoff", name="uq_weighting_cutoff"),
    )
    op.create_index("ix_weighting_lookup", "weighting_rounds", ["dataset", "cutoff", "id"])
    op.create_table(
        "weighting_heads",
        sa.Column("dataset", sa.String(80), primary_key=True),
        sa.Column("report_id", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("weighting_heads")
    op.drop_index("ix_weighting_lookup", table_name="weighting_rounds")
    op.drop_table("weighting_rounds")
