"""managed multi-day planning cascade

Revision ID: d4f6a8c2e901
Revises: c92a6f8d1e20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4f6a8c2e901"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    config_columns = (
        ("future_opportunity_critical", "750"),
        ("future_opportunity_high", "500"),
        ("future_opportunity_limited", "250"),
        ("batch_initial_horizon_days", "7"),
        ("batch_maximum_horizon_days", "30"),
        ("batch_total_time_limit_sec", "900"),
        ("max_jobs_per_batch", "5000"),
        ("solver_seed", "1"),
    )
    for name, default in config_columns:
        op.add_column(
            "planning_config",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )

    op.create_table(
        "planning_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requested_start_date", sa.Date(), nullable=False),
        sa.Column("effective_start_date", sa.Date(), nullable=False),
        sa.Column("initial_horizon_end", sa.Date(), nullable=False),
        sa.Column("maximum_horizon_end", sa.Date(), nullable=False),
        sa.Column("processed_through_date", sa.Date()),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("completion_reason", sa.String(64)),
        sa.Column(
            "initiated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("configuration_version", sa.String(64), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("stop_requested_at", sa.DateTime(timezone=True)),
        sa.Column("current_flag", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "stale_for_publication",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_planning_batches_project_id"
        ),
    )
    op.create_index(
        "uq_active_planning_batch_project",
        "planning_batches",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('CREATED','PREPARING','RUNNING','STOP_REQUESTED')"
        ),
    )
    op.create_index(
        "uq_current_planning_batch_project",
        "planning_batches",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("current_flag = true"),
    )
    op.add_column("planning_runs", sa.Column("planning_batch_id", sa.BigInteger()))
    op.create_foreign_key(
        "fk_planning_runs_planning_batch_id_planning_batches",
        "planning_runs",
        "planning_batches",
        ["planning_batch_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_table(
        "planning_batch_days",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "planning_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("block_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "planning_run_id", sa.BigInteger(), sa.ForeignKey("planning_runs.id")
        ),
        sa.Column("input_jobs_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "solver_candidates_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("assigned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unassigned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deferred_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dropped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_job_ids_hash", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint(
            "planning_batch_id",
            "planning_date",
            name="uq_planning_batch_days_planning_batch_id",
        ),
    )
    op.create_table(
        "planning_batch_jobs",
        sa.Column(
            "planning_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_batches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), primary_key=True
        ),
        sa.Column("snapshot_sla_date", sa.Date(), nullable=False),
        sa.Column("eligibility_status", sa.String(32), nullable=False),
        sa.Column("priority_group", sa.String(64)),
        sa.Column("future_opportunity_count", sa.Integer()),
        sa.Column("future_opportunity_rank", sa.Integer()),
        sa.Column("future_opportunity_bonus", sa.Integer()),
        sa.Column("daily_drop_penalty", sa.BigInteger()),
        sa.Column("cascade_drop_penalty", sa.BigInteger()),
        sa.Column("processing_status", sa.String(40), nullable=False),
        sa.Column("assigned_date", sa.Date()),
        sa.Column(
            "planning_run_id", sa.BigInteger(), sa.ForeignKey("planning_runs.id")
        ),
        sa.Column("primary_reason_code", sa.String(64)),
        sa.Column(
            "diagnostic_flags", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("last_considered_date", sa.Date()),
        sa.Column(
            "last_planning_run_id", sa.BigInteger(), sa.ForeignKey("planning_runs.id")
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("planning_batch_jobs")
    op.drop_table("planning_batch_days")
    op.drop_constraint(
        "fk_planning_runs_planning_batch_id_planning_batches",
        "planning_runs",
        type_="foreignkey",
    )
    op.drop_column("planning_runs", "planning_batch_id")
    op.drop_index("uq_current_planning_batch_project", table_name="planning_batches")
    op.drop_index("uq_active_planning_batch_project", table_name="planning_batches")
    op.drop_table("planning_batches")
    for name in (
        "solver_seed",
        "max_jobs_per_batch",
        "batch_total_time_limit_sec",
        "batch_maximum_horizon_days",
        "batch_initial_horizon_days",
        "future_opportunity_limited",
        "future_opportunity_high",
        "future_opportunity_critical",
    ):
        op.drop_column("planning_config", name)
