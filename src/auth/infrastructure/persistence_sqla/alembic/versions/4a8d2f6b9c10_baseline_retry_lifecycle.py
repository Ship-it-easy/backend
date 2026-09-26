"""baseline retry lifecycle

Revision ID: 4a8d2f6b9c10
Revises: 91d6c3af04e2
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4a8d2f6b9c10"
down_revision: str | None = "91d6c3af04e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "planning_baseline_results",
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "planning_baseline_results",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "planning_baseline_results",
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE planning_baseline_results "
        "SET last_attempt_at = created_at, "
        "next_retry_at = CASE WHEN status = 'FAILED' "
        "THEN created_at + interval '2 minutes' ELSE NULL END"
    )


def downgrade() -> None:
    op.drop_column("planning_baseline_results", "last_attempt_at")
    op.drop_column("planning_baseline_results", "next_retry_at")
    op.drop_column("planning_baseline_results", "attempt_count")
