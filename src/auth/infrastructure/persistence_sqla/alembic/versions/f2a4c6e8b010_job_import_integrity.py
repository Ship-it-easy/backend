"""Add integrity snapshots for validated CSV imports.

Revision ID: f2a4c6e8b010
Revises: e37b19a5c200
"""

import sqlalchemy as sa
from alembic import op

revision = "f2a4c6e8b010"
down_revision = "e37b19a5c200"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "job_import_batches",
        sa.Column("normalized_payload_sha256", sa.String(64)),
    )
    op.add_column(
        "job_import_rows",
        sa.Column("work_type_name", sa.String(255)),
    )


def downgrade():
    op.drop_column("job_import_rows", "work_type_name")
    op.drop_column("job_import_batches", "normalized_payload_sha256")
