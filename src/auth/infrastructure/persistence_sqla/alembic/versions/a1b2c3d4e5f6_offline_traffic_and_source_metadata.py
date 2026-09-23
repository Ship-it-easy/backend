"""Add offline traffic settings and preserve source CSV metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a1b2c3d4e5f6"
down_revision = "f9c2d7a41b30"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "planning_config",
        sa.Column("traffic_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "planning_config",
        sa.Column("traffic_reliability_buffer", sa.Numeric(4, 2), nullable=False, server_default="1.15"),
    )
    op.add_column(
        "planning_config",
        sa.Column("traffic_profile", sa.String(64), nullable=False, server_default="moscow_default"),
    )
    op.add_column(
        "job_import_rows",
        sa.Column("source_type", sa.String(16), nullable=False, server_default="STANDARD"),
    )
    op.add_column(
        "job_import_rows",
        sa.Column("source_metadata_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column("job_import_rows", sa.Column("office_address", sa.Text()))


def downgrade():
    op.drop_column("job_import_rows", "office_address")
    op.drop_column("job_import_rows", "source_metadata_json")
    op.drop_column("job_import_rows", "source_type")
    op.drop_column("planning_config", "traffic_profile")
    op.drop_column("planning_config", "traffic_reliability_buffer")
    op.drop_column("planning_config", "traffic_enabled")
