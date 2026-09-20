"""immutable planning board read model

Revision ID: c1a7e9d42f60
Revises: f9c2d7a41b30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1a7e9d42f60"
down_revision: Union[str, None] = "f9c2d7a41b30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "planning_day_results",
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
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_job_ids_hash", sa.String(64)),
        sa.Column(
            "input_jobs_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("assigned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "unassigned_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("solver_status", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "plan_version_id",
            "planning_date",
            name="uq_planning_day_results_version_date",
        ),
    )
    op.create_index(
        "ix_planning_day_results_project_date",
        "planning_day_results",
        ["project_id", "planning_date"],
    )
    op.create_table(
        "planning_cancelled_job_snapshots",
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
        sa.Column(
            "engineer_id", sa.BigInteger(), sa.ForeignKey("engineers.id"), nullable=False
        ),
        sa.Column("previous_sequence", sa.Integer()),
        sa.Column("planned_start", sa.DateTime(timezone=True)),
        sa.Column("planned_finish", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cancelled_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
        ),
        sa.Column("cancelled_by_username", sa.String(255)),
        sa.Column(
            "job_snapshot",
            postgresql.JSONB(),
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
            "plan_version_id",
            "job_id",
            name="uq_planning_cancelled_snapshot_version_job",
        ),
    )
    op.create_index(
        "ix_planning_cancelled_snapshots_version_date",
        "planning_cancelled_job_snapshots",
        ["plan_version_id", "planning_date"],
    )
    op.execute("""
        INSERT INTO planning_day_results (
            plan_version_id, project_id, planning_date, planning_run_id,
            input_job_ids_hash, input_jobs_count, assigned_count,
            unassigned_count, solver_status
        )
        SELECT DISTINCT ON (v.id, r.planning_date)
            v.id, v.project_id, r.planning_date, r.id,
            r.normalized_input_hash, r.input_jobs_count,
            r.assigned_jobs_count, r.unassigned_jobs_count, r.solver_status
        FROM project_plan_versions v
        JOIN planning_runs r ON r.planning_batch_id = v.planning_batch_id
        WHERE r.status = 'SUCCESS'
        ORDER BY v.id, r.planning_date, r.id DESC
    """)
    op.execute("""
        INSERT INTO planning_cancelled_job_snapshots (
            plan_version_id, project_id, job_id, planning_date, engineer_id,
            previous_sequence,
            planned_start, planned_finish, cancelled_at,
            cancelled_by_user_id, cancelled_by_username, job_snapshot
        )
        SELECT DISTINCT ON (target_version.id, pc.job_id)
            target_version.id,
            pc.project_id,
            pc.job_id,
            (pc.old_assignment->>'planning_date')::date,
            (pc.old_assignment->>'engineer_id')::bigint,
            (pc.old_assignment->>'sequence')::integer,
            (pc.old_assignment->>'planned_start')::timestamptz,
            (pc.old_assignment->>'planned_finish')::timestamptz,
            COALESCE(j.cancelled_at, ppv.published_at),
            j.cancelled_by_user_id,
            u.username,
            jsonb_build_object(
                'id', j.id,
                'address', j.address,
                'sla_date', j.sla_date,
                'priority_type', j.priority_type,
                'service_duration_min',
                    COALESCE(j.service_duration_min, wt.default_service_duration_min),
                'work_type_id', j.work_type_id,
                'work_type_name', wt.name,
                'status', 'CANCELLED'
            )
        FROM plan_changes pc
        JOIN project_plan_versions ppv ON ppv.id = pc.plan_version_id
        JOIN project_plan_versions target_version
          ON target_version.project_id = pc.project_id
         AND target_version.version_number >= ppv.version_number
        JOIN jobs j ON j.id = pc.job_id
        JOIN work_types wt ON wt.id = j.work_type_id
        LEFT JOIN users u ON u.id = j.cancelled_by_user_id
        WHERE pc.change_type = 'CANCELLED'
          AND pc.old_assignment IS NOT NULL
          AND pc.old_assignment ? 'planning_date'
          AND pc.old_assignment ? 'engineer_id'
        ORDER BY target_version.id, pc.job_id, ppv.published_at DESC
        ON CONFLICT (plan_version_id, job_id) DO NOTHING
    """)


def downgrade() -> None:
    op.drop_index(
        "ix_planning_cancelled_snapshots_version_date",
        table_name="planning_cancelled_job_snapshots",
    )
    op.drop_table("planning_cancelled_job_snapshots")
    op.drop_index(
        "ix_planning_day_results_project_date", table_name="planning_day_results"
    )
    op.drop_table("planning_day_results")
