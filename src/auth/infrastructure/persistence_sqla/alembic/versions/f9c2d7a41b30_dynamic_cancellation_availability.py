"""dynamic cancellation, availability, emergency priority and distance objective

Revision ID: f9c2d7a41b30
Revises: a6d2f4c8b913
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9c2d7a41b30"
down_revision: Union[str, None] = "a6d2f4c8b913"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "priority_type", sa.String(16), nullable=False, server_default="NORMAL"
        ),
    )
    op.add_column("jobs", sa.Column("cancelled_at", sa.DateTime(timezone=True)))
    op.add_column(
        "jobs",
        sa.Column(
            "cancelled_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
        ),
    )
    op.add_column("jobs", sa.Column("previous_status", sa.String(32)))
    op.create_check_constraint(
        "jobs_priority_type_valid", "jobs", "priority_type IN ('NORMAL','EMERGENCY')"
    )

    op.add_column(
        "planning_config",
        sa.Column(
            "distance_unit_meters", sa.Integer(), nullable=False, server_default="10"
        ),
    )
    op.add_column(
        "planning_config",
        sa.Column(
            "time_unit_seconds", sa.Integer(), nullable=False, server_default="60"
        ),
    )
    op.add_column(
        "planning_events",
        sa.Column(
            "engineer_ids", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
    )
    op.add_column(
        "planning_events",
        sa.Column(
            "event_payload", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "planning_batch_jobs",
        sa.Column(
            "emergency_bonus", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column("planning_batch_jobs", sa.Column("solver_drop_cost", sa.BigInteger()))
    op.add_column(
        "project_plan_assignments",
        sa.Column(
            "distance_from_previous_meters",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "planning_routes",
        sa.Column(
            "total_distance_meters", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "planning_route_jobs",
        sa.Column(
            "distance_from_previous_meters",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("travel_time_cache", sa.Column("travel_time_seconds", sa.Integer()))
    op.add_column("travel_time_cache", sa.Column("distance_meters", sa.BigInteger()))
    op.execute(
        "UPDATE travel_time_cache SET travel_time_seconds = duration_min * 60 WHERE duration_min IS NOT NULL"
    )

    for name, column in (
        (
            "objective_range_snapshot",
            sa.Column(
                "objective_range_snapshot",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
            ),
        ),
        (
            "fixed_active_engineer_count",
            sa.Column(
                "fixed_active_engineer_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        ),
        (
            "newly_activated_engineer_count",
            sa.Column(
                "newly_activated_engineer_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        ),
        (
            "used_engineer_count",
            sa.Column(
                "used_engineer_count", sa.Integer(), nullable=False, server_default="0"
            ),
        ),
        (
            "total_distance_meters",
            sa.Column(
                "total_distance_meters",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        ),
        (
            "max_engineer_distance_meters",
            sa.Column(
                "max_engineer_distance_meters",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        ),
    ):
        op.add_column("planning_runs", column)

    op.create_table(
        "job_planning_state",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "source_event_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_events.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "plan_version_id",
            sa.BigInteger(),
            sa.ForeignKey("project_plan_versions.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint(
            "state IN ('UNASSIGNED','ASSIGNED')", name="job_planning_state_valid"
        ),
    )
    op.execute("""
        INSERT INTO job_planning_state (job_id, project_id, state, plan_version_id)
        SELECT j.id, j.project_id,
               CASE
                   WHEN j.status IN ('IN_PROGRESS', 'COMPLETED') THEN 'ASSIGNED'
                   WHEN ppa.id IS NOT NULL THEN 'ASSIGNED'
                   WHEN EXISTS (
                       SELECT 1
                       FROM assignments a
                       JOIN plan_versions pv ON pv.id = a.plan_version_id
                       JOIN daily_plans dp
                         ON dp.id = pv.daily_plan_id
                        AND dp.current_version_id = pv.id
                       WHERE a.job_id = j.id AND a.active = true
                   ) THEN 'ASSIGNED'
                   ELSE 'UNASSIGNED'
               END,
               ppv.id
        FROM jobs j
        LEFT JOIN project_plan_versions ppv ON ppv.project_id = j.project_id AND ppv.is_current = true
        LEFT JOIN project_plan_assignments ppa ON ppa.plan_version_id = ppv.id AND ppa.job_id = j.id
        WHERE j.status <> 'CANCELLED'
    """)
    op.create_table(
        "engineer_availability_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "engineer_id",
            sa.BigInteger(),
            sa.ForeignKey("engineers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("old_interval", postgresql.JSONB()),
        sa.Column("new_interval", postgresql.JSONB()),
        sa.Column(
            "actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("engineer_availability_events")
    op.drop_table("job_planning_state")
    for name in (
        "max_engineer_distance_meters",
        "total_distance_meters",
        "used_engineer_count",
        "newly_activated_engineer_count",
        "fixed_active_engineer_count",
        "objective_range_snapshot",
    ):
        op.drop_column("planning_runs", name)
    op.drop_column("travel_time_cache", "distance_meters")
    op.drop_column("travel_time_cache", "travel_time_seconds")
    op.drop_column("project_plan_assignments", "distance_from_previous_meters")
    op.drop_column("planning_route_jobs", "distance_from_previous_meters")
    op.drop_column("planning_routes", "total_distance_meters")
    op.drop_column("planning_batch_jobs", "solver_drop_cost")
    op.drop_column("planning_batch_jobs", "emergency_bonus")
    op.drop_column("planning_events", "event_payload")
    op.drop_column("planning_events", "engineer_ids")
    op.drop_column("planning_config", "time_unit_seconds")
    op.drop_column("planning_config", "distance_unit_meters")
    op.drop_constraint("jobs_priority_type_valid", "jobs", type_="check")
    op.drop_column("jobs", "previous_status")
    op.drop_column("jobs", "cancelled_by_user_id")
    op.drop_column("jobs", "cancelled_at")
    op.drop_column("jobs", "priority_type")
