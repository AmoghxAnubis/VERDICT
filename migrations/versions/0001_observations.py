"""Create append-only normalized observation storage."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "observations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("asset", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_observation_lookup", "observations", ["dataset", "asset", "source", "timestamp", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_observation_lookup", table_name="observations")
    op.drop_table("observations")
