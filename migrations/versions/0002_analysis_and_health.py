"""Receipt-time indexing, connector health, and immutable statistical rounds."""

import json
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("observations", sa.Column("received_at", sa.DateTime(timezone=True)))
    observations = sa.table(
        "observations",
        sa.column("id", sa.String),
        sa.column("payload", sa.Text),
        sa.column("received_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.select(observations.c.id, observations.c.payload)):
        received = datetime.fromisoformat(
            json.loads(row.payload)["received_at"].replace("Z", "+00:00")
        )
        connection.execute(
            observations.update().where(observations.c.id == row.id).values(received_at=received)
        )
    with op.batch_alter_table("observations") as batch:
        batch.alter_column("received_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.create_index(
        "ix_observation_cutoff", "observations", ["dataset", "asset", "received_at", "timestamp"]
    )
    op.create_table(
        "connector_health",
        sa.Column("source", sa.String(64), primary_key=True),
        sa.Column("asset", sa.String(16), primary_key=True),
        sa.Column("last_attempt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success", sa.DateTime(timezone=True)),
        sa.Column("last_failure", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("received", sa.Integer(), nullable=False),
        sa.Column("inserted", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_error_type", sa.String(128)),
        sa.Column("last_http_status", sa.Integer()),
    )
    op.create_table(
        "analysis_rounds",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("dataset", sa.String(80), nullable=False),
        sa.Column("asset", sa.String(16), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_round_lookup", "analysis_rounds", ["dataset", "asset", "cutoff", "id"])


def downgrade() -> None:
    op.drop_index("ix_round_lookup", table_name="analysis_rounds")
    op.drop_table("analysis_rounds")
    op.drop_table("connector_health")
    op.drop_index("ix_observation_cutoff", table_name="observations")
    with op.batch_alter_table("observations") as batch:
        batch.drop_column("received_at")
