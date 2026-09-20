"""Store operational metrics for CSV imports.

Revision ID: f3b5d7e9c121
Revises: f2a4c6e8b010
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3b5d7e9c121"
down_revision = "f2a4c6e8b010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "job_import_batches",
        sa.Column(
            "metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade():
    op.drop_column("job_import_batches", "metrics")
