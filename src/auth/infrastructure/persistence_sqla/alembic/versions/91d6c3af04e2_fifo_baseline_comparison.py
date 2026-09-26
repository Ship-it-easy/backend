"""Persist FIFO baseline comparison for planning runs.

Revision ID: 91d6c3af04e2
Revises: 7c4a1d9e2b60
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "91d6c3af04e2"
down_revision: Union[str, None] = "7c4a1d9e2b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("received_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("jobs", sa.Column("ingest_sequence", sa.Integer(), nullable=True))
    op.execute("UPDATE jobs SET received_at = created_at WHERE received_at IS NULL")
    op.execute(
        """
        UPDATE jobs AS job
        SET received_at = batch.created_at
        FROM job_import_batches AS batch
        WHERE job.import_batch_id = batch.id
        """
    )
    op.execute(
        """
        UPDATE jobs
        SET ingest_sequence = COALESCE(import_row_number - 1, 1)
        WHERE ingest_sequence IS NULL
        """
    )
    op.alter_column(
        "jobs", "received_at", nullable=False, server_default=sa.text("now()")
    )
    op.alter_column(
        "jobs", "ingest_sequence", nullable=False, server_default=sa.text("1")
    )
    op.create_check_constraint(
        "job_ingest_sequence_positive", "jobs", "ingest_sequence > 0"
    )

    op.create_table(
        "planning_baseline_results",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("algorithm_version", sa.String(length=32), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("travel_matrix_hash", sa.String(length=64)),
        sa.Column("result_hash", sa.String(length=64)),
        sa.Column("input_jobs_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "assigned_jobs_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "unassigned_jobs_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("window_hit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "window_miss_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("window_hit_rate", sa.Numeric(7, 4)),
        sa.Column(
            "active_engineer_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "total_distance_meters", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("earliest_shift_start", sa.DateTime(timezone=True)),
        sa.Column(
            "calculation_time_ms", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("failure_code", sa.String(length=64)),
        sa.Column("failure_message", sa.Text()),
        sa.Column(
            "result_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "distance_matrices",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "planning_run_id",
            "algorithm_version",
            name="uq_planning_baseline_run_algorithm",
        ),
    )
    op.create_index(
        "ix_planning_baseline_project_date",
        "planning_baseline_results",
        ["project_id", "planning_date"],
    )
    op.create_table(
        "planning_plan_comparisons",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "baseline_result_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("formula_version", sa.String(length=64), nullable=False),
        sa.Column("coverage_comparable", sa.Boolean(), nullable=False),
        sa.Column(
            "baseline_assigned_job_ids_hash", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "optimized_assigned_job_ids_hash", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "baseline_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "optimized_metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("deltas", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "engineer_metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "planning_run_id",
            "formula_version",
            name="uq_planning_comparison_run_formula",
        ),
    )
    op.create_index(
        "ix_planning_comparisons_project_run",
        "planning_plan_comparisons",
        ["project_id", "planning_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_planning_comparisons_project_run",
        table_name="planning_plan_comparisons",
    )
    op.drop_table("planning_plan_comparisons")
    op.drop_index(
        "ix_planning_baseline_project_date",
        table_name="planning_baseline_results",
    )
    op.drop_table("planning_baseline_results")
    op.drop_constraint("job_ingest_sequence_positive", "jobs", type_="check")
    op.drop_column("jobs", "ingest_sequence")
    op.drop_column("jobs", "received_at")
