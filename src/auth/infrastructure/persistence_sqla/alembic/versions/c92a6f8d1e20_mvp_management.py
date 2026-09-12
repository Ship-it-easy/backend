"""MVP project management, publication and engineer cabinet.

Revision ID: c92a6f8d1e20
Revises: b781a2d9c4ef
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c92a6f8d1e20"
down_revision: Union[str, None] = "b781a2d9c4ef"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL enums used by SQLAlchemy persist member names, not enum values.
    op.execute("ALTER TYPE userroleenum ADD VALUE IF NOT EXISTS 'OWNER'")
    op.execute("ALTER TYPE userroleenum ADD VALUE IF NOT EXISTS 'DISPATCHER'")
    op.execute("ALTER TYPE userroleenum ADD VALUE IF NOT EXISTS 'ENGINEER'")

    op.add_column("projects", sa.Column("internal_code", sa.String(64)))
    op.add_column("projects", sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"))
    op.add_column("projects", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    op.add_column("projects", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    op.execute("UPDATE projects SET internal_code = 'PRJ-' || id WHERE internal_code IS NULL")
    op.alter_column("projects", "internal_code", nullable=False)
    op.create_unique_constraint("uq_projects_internal_code", "projects", ["internal_code"])
    op.create_index("uq_projects_active_name_ci", "projects", [sa.text("lower(name)")], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))

    op.add_column("users", sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id")))
    op.add_column("users", sa.Column("engineer_id", sa.BigInteger(), sa.ForeignKey("engineers.id")))
    op.create_unique_constraint("uq_users_engineer_id", "users", ["engineer_id"])
    op.create_index("uq_users_username_ci", "users", [sa.text("lower(username)")], unique=True)

    op.add_column("work_types", sa.Column("active", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("qualifications", sa.Column("active", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("equipment_types", sa.Column("available_units", sa.Integer(), nullable=False, server_default="0"))
    op.execute(
        """
        UPDATE equipment_types AS target
        SET available_units = source.available_units
        FROM (
            SELECT DISTINCT ON (equipment_type_id)
                equipment_type_id, available_units
            FROM equipment_availability
            ORDER BY equipment_type_id, availability_date DESC
        ) AS source
        WHERE source.equipment_type_id = target.id
        """
    )
    op.create_check_constraint("ck_equipment_types_equipment_type_units_nonnegative", "equipment_types", "available_units >= 0")
    for table in ("work_types", "qualifications", "equipment_types"):
        op.create_index(f"uq_{table}_project_name_ci", table, ["project_id", sa.text("lower(name)")], unique=True)

    op.add_column("engineers", sa.Column("internal_code", sa.String(64)))
    op.execute("UPDATE engineers SET internal_code = 'ENG-' || id WHERE internal_code IS NULL")
    op.alter_column("engineers", "internal_code", nullable=False)
    op.create_unique_constraint("uq_engineers_project_internal_code", "engineers", ["project_id", "internal_code"])

    op.add_column("jobs", sa.Column("internal_code", sa.String(64)))
    op.execute("UPDATE jobs SET internal_code = 'JOB-' || id WHERE internal_code IS NULL")
    op.alter_column("jobs", "internal_code", nullable=False)
    op.create_unique_constraint("uq_jobs_project_internal_code", "jobs", ["project_id", "internal_code"])

    op.create_table(
        "daily_plans",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("project_id", sa.BigInteger(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("planning_date", sa.Date(), nullable=False),
        sa.Column("current_version_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "planning_date", name="uq_daily_plans_project_date"),
    )
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("daily_plan_id", sa.BigInteger(), sa.ForeignKey("daily_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("planning_run_id", sa.BigInteger(), sa.ForeignKey("planning_runs.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="PUBLISHED"),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("superseded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("daily_plan_id", "version_number", name="uq_plan_versions_number"),
    )
    op.create_foreign_key("fk_daily_plans_current_version", "daily_plans", "plan_versions", ["current_version_id"], ["id"])
    op.create_table(
        "assignments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("plan_version_id", sa.BigInteger(), sa.ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("engineer_id", sa.BigInteger(), sa.ForeignKey("engineers.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_finish", sa.DateTime(timezone=True), nullable=False),
        sa.Column("route_data", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("requirement_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("plan_version_id", "job_id", name="uq_assignments_version_job"),
        sa.UniqueConstraint("plan_version_id", "engineer_id", "sequence", name="uq_assignments_version_engineer_sequence"),
    )
    op.create_table(
        "job_status_history",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("job_id", sa.BigInteger(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assignment_id", sa.BigInteger(), sa.ForeignKey("assignments.id")),
        sa.Column("old_status", sa.String(32), nullable=False),
        sa.Column("new_status", sa.String(32), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("job_status_history")
    op.drop_table("assignments")
    op.drop_constraint("fk_daily_plans_current_version", "daily_plans", type_="foreignkey")
    op.drop_table("plan_versions")
    op.drop_table("daily_plans")
    op.drop_constraint("uq_jobs_project_internal_code", "jobs", type_="unique")
    op.drop_column("jobs", "internal_code")
    op.drop_constraint("uq_engineers_project_internal_code", "engineers", type_="unique")
    op.drop_column("engineers", "internal_code")
    for table in ("work_types", "qualifications", "equipment_types"):
        op.drop_index(f"uq_{table}_project_name_ci", table_name=table)
    op.drop_constraint("ck_equipment_types_equipment_type_units_nonnegative", "equipment_types", type_="check")
    op.drop_column("equipment_types", "available_units")
    op.drop_column("qualifications", "active")
    op.drop_column("work_types", "active")
    op.drop_constraint("uq_users_engineer_id", "users", type_="unique")
    op.drop_index("uq_users_username_ci", table_name="users")
    op.drop_column("users", "engineer_id")
    op.drop_column("users", "project_id")
    op.drop_index("uq_projects_active_name_ci", table_name="projects")
    op.drop_constraint("uq_projects_internal_code", "projects", type_="unique")
    op.drop_column("projects", "updated_at")
    op.drop_column("projects", "created_at")
    op.drop_column("projects", "status")
    op.drop_column("projects", "internal_code")
