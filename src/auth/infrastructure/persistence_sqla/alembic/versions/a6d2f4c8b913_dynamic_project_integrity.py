"""Strengthen project-wide plan integrity and status audit links.

Revision ID: a6d2f4c8b913
Revises: e8b4c7a91d22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a6d2f4c8b913"
down_revision: str | None = "e8b4c7a91d22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_status_history",
        sa.Column("project_plan_assignment_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_job_history_project_assignment",
        "job_status_history",
        "project_plan_assignments",
        ["project_plan_assignment_id"],
        ["id"],
    )

    op.drop_constraint(
        "uq_candidate_evaluation_event_job_engineer",
        "candidate_evaluations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_candidate_evaluation_event_job_engineer",
        "candidate_evaluations",
        ["project_id", "planning_event_id", "subject_job_id", "engineer_id"],
    )

    op.drop_constraint(
        "uq_project_plan_assignment_job",
        "project_plan_assignments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_project_plan_assignment_job",
        "project_plan_assignments",
        ["project_id", "plan_version_id", "job_id"],
    )
    op.drop_constraint(
        "uq_project_plan_assignment_sequence",
        "project_plan_assignments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_project_plan_assignment_sequence",
        "project_plan_assignments",
        [
            "project_id",
            "plan_version_id",
            "planning_date",
            "engineer_id",
            "sequence",
        ],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_project_plan_assignment_sequence",
        "project_plan_assignments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_project_plan_assignment_sequence",
        "project_plan_assignments",
        ["plan_version_id", "planning_date", "engineer_id", "sequence"],
    )
    op.drop_constraint(
        "uq_project_plan_assignment_job",
        "project_plan_assignments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_project_plan_assignment_job",
        "project_plan_assignments",
        ["plan_version_id", "job_id"],
    )

    op.drop_constraint(
        "uq_candidate_evaluation_event_job_engineer",
        "candidate_evaluations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_candidate_evaluation_event_job_engineer",
        "candidate_evaluations",
        ["planning_event_id", "subject_job_id", "engineer_id"],
    )

    op.drop_constraint(
        "fk_job_history_project_assignment",
        "job_status_history",
        type_="foreignkey",
    )
    op.drop_column("job_status_history", "project_plan_assignment_id")
