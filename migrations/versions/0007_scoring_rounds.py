"""Persist diagnostic source scoring with pinned dependency evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scoring_rounds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registry_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_scoring_lookup", "scoring_rounds", ["dataset", "cutoff", "id"])


def downgrade() -> None:
    op.drop_index("ix_scoring_lookup", table_name="scoring_rounds")
    op.drop_table("scoring_rounds")
