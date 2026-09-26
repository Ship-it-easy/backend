"""baseline route audit rows

Revision ID: 6c9e1a4d7b22
Revises: 4a8d2f6b9c10
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6c9e1a4d7b22"
down_revision: str | None = "4a8d2f6b9c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The original rollout may already have backfilled imported jobs from their
    # individual insert timestamps. Restore the required package-wide FIFO time.
    op.execute(
        """
        UPDATE jobs AS job
        SET received_at = batch.created_at
        FROM job_import_batches AS batch
        WHERE job.import_batch_id = batch.id
        """
    )
    op.add_column(
        "planning_runs",
        sa.Column(
            "travel_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "planning_baseline_results",
        sa.Column("plan_version_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_planning_baseline_results_plan_version",
        "planning_baseline_results",
        "project_plan_versions",
        ["plan_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "planning_baseline_results",
        sa.Column(
            "snapshot_version",
            sa.String(length=32),
            server_default="DAY_INPUT_V1",
            nullable=False,
        ),
    )
    op.add_column(
        "planning_baseline_results",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "planning_baseline_results",
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "uq_planning_baseline_run_algorithm",
        "planning_baseline_results",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_planning_baseline_run_algorithm",
        "planning_baseline_results",
        ["project_id", "planning_run_id", "algorithm_version"],
    )
    op.create_table(
        "planning_baseline_routes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "baseline_result_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("engineer_id", sa.BigInteger(), sa.ForeignKey("engineers.id"), nullable=False),
        sa.Column("engineer_input_order", sa.Integer(), nullable=False),
        sa.Column("shift_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shift_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity_minutes", sa.Integer(), nullable=False),
        sa.Column("assigned_jobs_count", sa.Integer(), nullable=False),
        sa.Column("consumed_minutes", sa.Integer(), nullable=False),
        sa.Column("remaining_minutes", sa.Integer(), nullable=False),
        sa.Column("distance_meters", sa.BigInteger(), nullable=False),
        sa.Column(
            "start_location_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "baseline_result_id",
            "engineer_id",
            name="uq_planning_baseline_route_engineer",
        ),
    )
    op.create_table(
        "planning_baseline_route_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "baseline_result_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "baseline_route_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_baseline_routes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("job_input_order", sa.Integer(), nullable=False),
        sa.Column("route_position", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_sequence", sa.Integer(), nullable=False),
        sa.Column("service_duration_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "standard_travel_minutes", sa.Integer(), server_default="20", nullable=False
        ),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_finish", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_from_min", sa.Integer(), nullable=False),
        sa.Column("window_to_min", sa.Integer(), nullable=False),
        sa.Column("window_hit", sa.Boolean(), nullable=False),
        sa.Column("previous_location_type", sa.String(length=24), nullable=False),
        sa.Column("distance_from_previous_meters", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint(
            "baseline_result_id",
            "job_id",
            name="uq_planning_baseline_result_job",
        ),
        sa.UniqueConstraint(
            "baseline_route_id",
            "route_position",
            name="uq_planning_baseline_route_position",
        ),
    )
    op.create_table(
        "planning_baseline_unassigned_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "baseline_result_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("job_input_order", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column(
            "diagnostics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.UniqueConstraint(
            "baseline_result_id",
            "job_id",
            name="uq_planning_baseline_unassigned_job",
        ),
    )


def downgrade() -> None:
    op.drop_table("planning_baseline_unassigned_jobs")
    op.drop_table("planning_baseline_route_jobs")
    op.drop_table("planning_baseline_routes")
    op.drop_constraint(
        "uq_planning_baseline_run_algorithm",
        "planning_baseline_results",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_planning_baseline_run_algorithm",
        "planning_baseline_results",
        ["planning_run_id", "algorithm_version"],
    )
    op.drop_column("planning_baseline_results", "finished_at")
    op.drop_column("planning_baseline_results", "started_at")
    op.drop_column("planning_baseline_results", "snapshot_version")
    op.drop_constraint(
        "fk_planning_baseline_results_plan_version",
        "planning_baseline_results",
        type_="foreignkey",
    )
    op.drop_column("planning_baseline_results", "plan_version_id")
    op.drop_column("planning_runs", "travel_snapshot")
