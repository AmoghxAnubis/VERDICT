"""Retain immutable, evidence-backed dependency registry versions."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dependency_registries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(96), nullable=False, unique=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("dependency_registries")
