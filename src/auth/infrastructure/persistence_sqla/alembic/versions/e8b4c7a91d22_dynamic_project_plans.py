"""dynamic planning events and project-wide published plans

Revision ID: e8b4c7a91d22
Revises: d4f6a8c2e901
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8b4c7a91d22"
down_revision: Union[str, None] = "d4f6a8c2e901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "planning_config",
        sa.Column(
            "nightly_planning_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "candidate_solver_time_limit_sec",
            sa.Integer(),
            nullable=False,
            server_default="20",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "event_time_limit_sec",
            sa.Integer(),
            nullable=False,
            server_default="1200",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "single_cascade_time_limit_sec",
            sa.Integer(),
            nullable=False,
            server_default="900",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "event_coalesce_window_sec",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "event_coalesce_max_wait_sec",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "max_parallel_candidate_models",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "nightly_planning_time",
            sa.Time(),
            nullable=False,
            server_default="02:00:00",
        ),
    )
    op.alter_column("planning_batches", "initiated_by_user_id", nullable=True)
    op.create_table(
        "planning_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("job_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("initiator", sa.String(16), nullable=False),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")
        ),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column(
            "planning_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_batches.id", ondelete="SET NULL"),
        ),
        sa.Column("input_hash", sa.String(64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidate_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "current_candidate_engineer_id",
            sa.BigInteger(),
            sa.ForeignKey("engineers.id"),
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name="uq_planning_events_idempotency"
        ),
    )
    op.create_index(
        "ix_planning_events_pending",
        "planning_events",
        ["project_id", "state"],
    )

    op.create_table(
        "candidate_evaluations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "planning_event_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "planning_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_batches.id", ondelete="CASCADE"),
        ),
        sa.Column("subject_job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("engineer_id", sa.BigInteger(), sa.ForeignKey("engineers.id"), nullable=False),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("solver_status", sa.String(64), nullable=False),
        sa.Column("validator_status", sa.String(32), nullable=False),
        sa.Column("rejection_reason", sa.String(128)),
        sa.Column("original_route", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("result_route", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("dropped_job_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("score_vector", postgresql.JSONB()),
        sa.Column("travel_matrix_hash", sa.String(64)),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("solver_time_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "planning_event_id",
            "subject_job_id",
            "engineer_id",
            name="uq_candidate_evaluation_event_job_engineer",
        ),
    )

    op.create_table(
        "project_plan_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column(
            "planning_batch_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_batches.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "planning_event_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_events.id", ondelete="SET NULL"),
        ),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("trigger_source", sa.String(32), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("unassigned_jobs", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "project_id", "version_number", name="uq_project_plan_version_number"
        ),
    )
    op.create_index(
        "uq_project_plan_versions_current",
        "project_plan_versions",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
    )

    op.create_table(
        "project_plan_assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.BigInteger(),
            sa.ForeignKey("project_plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("engineer_id", sa.BigInteger(), sa.ForeignKey("engineers.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("planned_arrival", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_finish", sa.DateTime(timezone=True), nullable=False),
        sa.Column("travel_from_previous_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("waiting_before_job_min", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requirement_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint(
            "plan_version_id",
            "job_id",
            name="uq_project_plan_assignment_job",
        ),
        sa.UniqueConstraint(
            "plan_version_id",
            "planning_date",
            "engineer_id",
            "sequence",
            name="uq_project_plan_assignment_sequence",
        ),
    )
    op.create_index(
        "ix_project_plan_assignments_project_job",
        "project_plan_assignments",
        ["project_id", "job_id"],
    )

    op.create_table(
        "plan_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "plan_version_id",
            sa.BigInteger(),
            sa.ForeignKey("project_plan_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("old_assignment", postgresql.JSONB()),
        sa.Column("new_assignment", postgresql.JSONB()),
        sa.Column("reason", sa.String(128), nullable=False),
    )
    op.create_index("ix_plan_changes_version", "plan_changes", ["plan_version_id"])
    op.add_column(
        "planning_events",
        sa.Column("published_plan_version_id", sa.BigInteger()),
    )
    op.create_foreign_key(
        "fk_planning_events_published_plan_version",
        "planning_events",
        "project_plan_versions",
        ["published_plan_version_id"],
        ["id"],
        use_alter=True,
    )

    # Preserve the currently visible legacy assignments as version 1. Historical
    # daily versions remain in their original tables for audit.
    op.execute(
        """
        INSERT INTO project_plan_versions
            (project_id, version_number, input_hash, trigger_source, is_current,
             published_at, unassigned_jobs, metrics)
        SELECT dp.project_id, 1, md5('legacy:' || dp.project_id::text),
               'MIGRATION', true, max(pv.published_at), '[]'::jsonb, '{}'::jsonb
        FROM daily_plans dp
        JOIN plan_versions pv ON pv.id = dp.current_version_id
        GROUP BY dp.project_id
        """
    )
    op.execute(
        """
        INSERT INTO project_plan_assignments
            (plan_version_id, project_id, job_id, planning_date, engineer_id,
             sequence, planned_arrival, planned_start, planned_finish,
             travel_from_previous_min, waiting_before_job_min, requirement_snapshot)
        SELECT DISTINCT ON (dp.project_id, a.job_id)
               ppv.id, dp.project_id, a.job_id, dp.planning_date, a.engineer_id,
               a.sequence,
               COALESCE((a.route_data->>'planned_arrival')::timestamptz, a.planned_start),
               a.planned_start, a.planned_finish,
               COALESCE((a.route_data->>'travel_from_previous_min')::integer, 0),
               COALESCE((a.route_data->>'waiting_before_job_min')::integer, 0),
               a.requirement_snapshot
        FROM daily_plans dp
        JOIN plan_versions pv ON pv.id = dp.current_version_id
        JOIN assignments a ON a.plan_version_id = pv.id AND a.active = true
        JOIN project_plan_versions ppv
          ON ppv.project_id = dp.project_id AND ppv.is_current = true
        ORDER BY dp.project_id, a.job_id, dp.planning_date
        """
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_planning_events_published_plan_version",
        "planning_events",
        type_="foreignkey",
    )
    op.drop_column("planning_events", "published_plan_version_id")
    op.drop_index("ix_plan_changes_version", table_name="plan_changes")
    op.drop_table("plan_changes")
    op.drop_index(
        "ix_project_plan_assignments_project_job",
        table_name="project_plan_assignments",
    )
    op.drop_table("project_plan_assignments")
    op.drop_index(
        "uq_project_plan_versions_current", table_name="project_plan_versions"
    )
    op.drop_table("project_plan_versions")
    op.drop_table("candidate_evaluations")
    op.drop_index("ix_planning_events_pending", table_name="planning_events")
    op.drop_table("planning_events")
    op.alter_column("planning_batches", "initiated_by_user_id", nullable=False)
    op.drop_column("planning_config", "nightly_planning_time")
    op.drop_column("planning_config", "nightly_planning_enabled")
    op.drop_column("planning_config", "event_time_limit_sec")
    op.drop_column("planning_config", "single_cascade_time_limit_sec")
    op.drop_column("planning_config", "event_coalesce_window_sec")
    op.drop_column("planning_config", "event_coalesce_max_wait_sec")
    op.drop_column("planning_config", "max_parallel_candidate_models")
    op.drop_column("planning_config", "candidate_solver_time_limit_sec")
