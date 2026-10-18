"""Sequence ordering for provider updates sharing an event timestamp."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "observations",
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
    )
    observations = sa.table(
        "observations",
        sa.column("id", sa.String),
        sa.column("payload", sa.Text),
        sa.column("event_sequence", sa.BigInteger),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.select(observations.c.id, observations.c.payload)):
        sequence = json.loads(row.payload).get("event_sequence", 0)
        if sequence:
            connection.execute(
                observations.update()
                .where(observations.c.id == row.id)
                .values(event_sequence=sequence)
            )
    with op.batch_alter_table("observations") as batch:
        batch.alter_column("event_sequence", existing_type=sa.BigInteger(), server_default=None)
    op.drop_index("ix_observation_lookup", table_name="observations")
    op.create_index(
        "ix_observation_lookup",
        "observations",
        ["dataset", "asset", "source", "timestamp", "event_sequence", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_observation_lookup", table_name="observations")
    op.create_index(
        "ix_observation_lookup", "observations", ["dataset", "asset", "source", "timestamp", "id"]
    )
    with op.batch_alter_table("observations") as batch:
        batch.drop_column("event_sequence")
