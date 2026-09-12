"""one day planning

Revision ID: b781a2d9c4ef
Revises: 8776e362ab52
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b781a2d9c4ef"
down_revision: Union[str, None] = "8776e362ab52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("planning_timezone", sa.String(64), nullable=False),
        sa.Column(
            "planning_one_day_enabled",
            sa.Boolean(),
            server_default="true",
            nullable=False,
        ),
    )
    op.create_table(
        "work_types",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("default_service_duration_min", sa.Integer()),
        sa.Column("required_transport", sa.String(16)),
        sa.CheckConstraint(
            "default_service_duration_min IS NULL OR default_service_duration_min > 0",
            name="ck_work_types_work_type_duration_positive",
        ),
        sa.UniqueConstraint("project_id", "code", name="uq_work_types_project_id"),
    )
    op.create_table(
        "qualifications",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.UniqueConstraint("project_id", "code", name="uq_qualifications_project_id"),
    )
    op.create_table(
        "equipment_types",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.UniqueConstraint("project_id", "code", name="uq_equipment_types_project_id"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("address_hash", sa.String(64)),
        sa.Column("latitude", sa.Numeric(9, 6)),
        sa.Column("longitude", sa.Numeric(9, 6)),
        sa.Column("geocoded_at", sa.DateTime(timezone=True)),
        sa.Column("sla_date", sa.Date(), nullable=False),
        sa.Column("time_window_start", sa.Time()),
        sa.Column("time_window_end", sa.Time()),
        sa.Column(
            "work_type_id",
            sa.BigInteger(),
            sa.ForeignKey("work_types.id"),
            nullable=False,
        ),
        sa.Column("service_duration_min", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "service_duration_min IS NULL OR service_duration_min > 0",
            name="ck_jobs_job_duration_positive",
        ),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name="ck_jobs_job_coordinate_pair",
        ),
        sa.UniqueConstraint("project_id", "external_id", name="uq_jobs_project_id"),
    )
    op.create_table(
        "engineers",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("transport_type", sa.String(16), nullable=False),
        sa.Column("start_address", sa.Text()),
        sa.Column("start_latitude", sa.Numeric(9, 6)),
        sa.Column("start_longitude", sa.Numeric(9, 6)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(start_latitude IS NULL) = (start_longitude IS NULL)",
            name="ck_engineers_engineer_coordinate_pair",
        ),
        sa.CheckConstraint(
            "start_address IS NOT NULL OR start_latitude IS NOT NULL",
            name="ck_engineers_engineer_start_present",
        ),
    )
    op.create_table(
        "engineer_schedules",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "engineer_id",
            sa.BigInteger(),
            sa.ForeignKey("engineers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("shift_start", sa.Time(), nullable=False),
        sa.Column("shift_end", sa.Time(), nullable=False),
        sa.CheckConstraint(
            "shift_start < shift_end", name="ck_engineer_schedules_schedule_same_day"
        ),
        sa.UniqueConstraint(
            "engineer_id", "work_date", name="uq_engineer_schedules_engineer_id"
        ),
    )
    op.create_table(
        "engineer_qualifications",
        sa.Column(
            "engineer_id",
            sa.BigInteger(),
            sa.ForeignKey("engineers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "qualification_id",
            sa.BigInteger(),
            sa.ForeignKey("qualifications.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "work_type_required_qualifications",
        sa.Column(
            "work_type_id",
            sa.BigInteger(),
            sa.ForeignKey("work_types.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "qualification_id",
            sa.BigInteger(),
            sa.ForeignKey("qualifications.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "work_type_required_equipment",
        sa.Column(
            "work_type_id",
            sa.BigInteger(),
            sa.ForeignKey("work_types.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "equipment_type_id",
            sa.BigInteger(),
            sa.ForeignKey("equipment_types.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "equipment_availability",
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "equipment_type_id",
            sa.BigInteger(),
            sa.ForeignKey("equipment_types.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("availability_date", sa.Date(), primary_key=True),
        sa.Column("available_units", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "available_units >= 0",
            name="ck_equipment_availability_equipment_units_nonnegative",
        ),
    )
    _create_planning_tables()
    _create_cache_tables()


def _create_planning_tables() -> None:
    op.create_table(
        "planning_config",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "sla_overdue_base", sa.Integer(), server_default="10000", nullable=False
        ),
        sa.Column(
            "sla_overdue_per_day", sa.Integer(), server_default="1000", nullable=False
        ),
        sa.Column("sla_today", sa.Integer(), server_default="9000", nullable=False),
        sa.Column("sla_tomorrow", sa.Integer(), server_default="4000", nullable=False),
        sa.Column("sla_2_3_days", sa.Integer(), server_default="2000", nullable=False),
        sa.Column("sla_later", sa.Integer(), server_default="500", nullable=False),
        sa.Column(
            "skill_one_engineer", sa.Integer(), server_default="500", nullable=False
        ),
        sa.Column(
            "skill_two_engineers", sa.Integer(), server_default="250", nullable=False
        ),
        sa.Column(
            "equipment_one_unit", sa.Integer(), server_default="500", nullable=False
        ),
        sa.Column(
            "equipment_two_units", sa.Integer(), server_default="250", nullable=False
        ),
        sa.Column("window_30", sa.Integer(), server_default="300", nullable=False),
        sa.Column("window_60", sa.Integer(), server_default="150", nullable=False),
        sa.Column("window_120", sa.Integer(), server_default="50", nullable=False),
        sa.Column(
            "travel_cost_per_minute", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column(
            "solver_time_limit_sec", sa.Integer(), server_default="60", nullable=False
        ),
        sa.Column(
            "max_jobs_per_run", sa.Integer(), server_default="1000", nullable=False
        ),
        sa.Column(
            "travel_provider",
            sa.String(32),
            server_default="VALHALLA_LOCAL",
            nullable=False,
        ),
        sa.Column("travel_cache_ttl_days", sa.Integer()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "version", name="uq_planning_config_project_id"
        ),
    )
    op.create_index(
        "uq_planning_config_active_project",
        "planning_config",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
    )
    op.create_table(
        "planning_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column(
            "initiated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("solver_status", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("objective", sa.BigInteger()),
        sa.Column("drop_cost", sa.BigInteger()),
        sa.Column("travel_cost", sa.BigInteger()),
        sa.Column("input_jobs_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "eligible_jobs_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "assigned_jobs_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "unassigned_jobs_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "sla_critical_total", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "sla_critical_assigned", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column(
            "config_snapshot", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column(
            "input_snapshot", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.Column("normalized_input_hash", sa.String(64)),
        sa.Column("travel_matrix_hash", sa.String(64)),
        sa.Column("traffic_reference_time", sa.DateTime(timezone=True)),
        sa.Column("solver_version", sa.String(64)),
        sa.Column("solver_time_ms", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "validation_errors", postgresql.JSONB(), server_default="[]", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_active_planning_run",
        "planning_runs",
        ["project_id", "planning_date"],
        unique=True,
        postgresql_where=sa.text("status IN ('CREATED','PREPARING','RUNNING')"),
    )
    op.create_table(
        "planning_routes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "engineer_id",
            sa.BigInteger(),
            sa.ForeignKey("engineers.id"),
            nullable=False,
        ),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_finish", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_travel_min", sa.Integer(), nullable=False),
        sa.Column("total_service_min", sa.Integer(), nullable=False),
        sa.Column("total_waiting_min", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "planning_run_id", "engineer_id", name="uq_planning_routes_planning_run_id"
        ),
    )
    op.create_table(
        "planning_route_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "planning_route_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_routes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("planned_arrival", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_finish", sa.DateTime(timezone=True), nullable=False),
        sa.Column("travel_from_previous_min", sa.Integer(), nullable=False),
        sa.Column("waiting_before_job_min", sa.Integer(), nullable=False),
        sa.Column("drop_penalty_snapshot", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "planning_route_id",
            "sequence",
            name="uq_planning_route_jobs_planning_route_id",
        ),
        sa.UniqueConstraint(
            "planning_run_id", "job_id", name="uq_planning_route_jobs_planning_run_id"
        ),
    )
    op.create_table(
        "planning_equipment_assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "engineer_id",
            sa.BigInteger(),
            sa.ForeignKey("engineers.id"),
            nullable=False,
        ),
        sa.Column(
            "equipment_type_id",
            sa.BigInteger(),
            sa.ForeignKey("equipment_types.id"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "planning_run_id",
            "engineer_id",
            "equipment_type_id",
            name="uq_planning_equipment_assignments_planning_run_id",
        ),
    )
    op.create_table(
        "planning_unassigned_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "planning_run_id",
            sa.BigInteger(),
            sa.ForeignKey("planning_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("drop_penalty", sa.Integer(), nullable=False),
        sa.Column("primary_reason_code", sa.String(64), nullable=False),
        sa.Column(
            "diagnostic_flags", postgresql.JSONB(), server_default="{}", nullable=False
        ),
        sa.UniqueConstraint(
            "planning_run_id",
            "job_id",
            name="uq_planning_unassigned_jobs_planning_run_id",
        ),
    )


def _create_cache_tables() -> None:
    op.create_table(
        "geocoding_cache",
        sa.Column("address_hash", sa.String(64), primary_key=True),
        sa.Column("normalized_address", sa.Text(), nullable=False),
        sa.Column("latitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("longitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_version", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_table(
        "travel_time_cache",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("origin_latitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("origin_longitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("destination_latitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("destination_longitude", sa.Numeric(9, 6), nullable=False),
        sa.Column("profile", sa.String(32), nullable=False),
        sa.Column("duration_min", sa.Integer()),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_version", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    for name in (
        "travel_time_cache",
        "geocoding_cache",
        "planning_unassigned_jobs",
        "planning_equipment_assignments",
        "planning_route_jobs",
        "planning_routes",
        "planning_runs",
        "planning_config",
        "equipment_availability",
        "work_type_required_equipment",
        "work_type_required_qualifications",
        "engineer_qualifications",
        "engineer_schedules",
        "engineers",
        "jobs",
        "equipment_types",
        "qualifications",
        "work_types",
        "projects",
    ):
        op.drop_table(name)
