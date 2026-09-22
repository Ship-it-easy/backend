"""move request priority to work types

Revision ID: 1b8e6d4a2c70
Revises: f3b5d7e9c121
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1b8e6d4a2c70"
down_revision: Union[str, None] = "f3b5d7e9c121"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "work_types",
        sa.Column("priority", sa.String(16), nullable=False, server_default="LOW"),
    )
    # Product requirement: every request which existed before this migration
    # must start with the lowest priority. Requests inherit it from work types.
    op.execute("UPDATE work_types SET priority = 'LOW'")
    op.create_check_constraint(
        "work_type_priority_valid",
        "work_types",
        "priority IN ('CRITICAL','HIGH','MEDIUM','LOW')",
    )

    op.alter_column(
        "planning_batch_jobs",
        "emergency_bonus",
        new_column_name="priority_bonus",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default="0",
    )
    op.execute("UPDATE planning_batch_jobs SET priority_bonus = 0")

    op.drop_constraint("jobs_priority_type_valid", "jobs", type_="check")
    op.drop_column("jobs", "priority_type")


def downgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "priority_type", sa.String(16), nullable=False, server_default="NORMAL"
        ),
    )
    op.create_check_constraint(
        "jobs_priority_type_valid",
        "jobs",
        "priority_type IN ('NORMAL','EMERGENCY')",
    )
    op.alter_column(
        "planning_batch_jobs",
        "priority_bonus",
        new_column_name="emergency_bonus",
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default="0",
    )
    op.drop_constraint("work_type_priority_valid", "work_types", type_="check")
    op.drop_column("work_types", "priority")
