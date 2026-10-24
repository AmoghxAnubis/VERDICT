"""Durable, commit-ordered diagnostic round notifications."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    head = op.create_table(
        "round_event_head",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
    )
    op.bulk_insert(head, [{"id": 1, "sequence": 0}])
    op.create_table(
        "round_events",
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("assessment_id", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_round_event_dataset", "round_events", ["dataset", "sequence"])
    # Existing assessments remain available via snapshots/history. The stream begins
    # at this migration; it does not pretend older saves happened in a known order.


def downgrade() -> None:
    op.drop_index("ix_round_event_dataset", table_name="round_events")
    op.drop_table("round_events")
    op.drop_table("round_event_head")
