from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from auth.infrastructure.persistence_sqla.orm_registry import metadata_obj

projects = Table(
    "projects",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("planning_timezone", String(64), nullable=False),
    Column("planning_one_day_enabled", Boolean, nullable=False, server_default="true"),
)

work_types = Table(
    "work_types",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("default_service_duration_min", Integer),
    Column("required_transport", String(16)),
    UniqueConstraint("project_id", "code"),
    CheckConstraint(
        "default_service_duration_min IS NULL OR default_service_duration_min > 0",
        name="work_type_duration_positive",
    ),
)

jobs = Table(
    "jobs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(255)),
    Column("status", String(32), nullable=False),
    Column("address", Text, nullable=False),
    Column("address_hash", String(64)),
    Column("latitude", Numeric(9, 6)),
    Column("longitude", Numeric(9, 6)),
    Column("geocoded_at", DateTime(timezone=True)),
    Column("sla_date", Date, nullable=False),
    Column("time_window_start", Time),
    Column("time_window_end", Time),
    Column("work_type_id", ForeignKey("work_types.id"), nullable=False),
    Column("service_duration_min", Integer),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("project_id", "external_id"),
    CheckConstraint(
        "service_duration_min IS NULL OR service_duration_min > 0",
        name="job_duration_positive",
    ),
    CheckConstraint(
        "(latitude IS NULL) = (longitude IS NULL)", name="job_coordinate_pair"
    ),
)

engineers = Table(
    "engineers",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(255), nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("transport_type", String(16), nullable=False),
    Column("start_address", Text),
    Column("start_latitude", Numeric(9, 6)),
    Column("start_longitude", Numeric(9, 6)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    CheckConstraint(
        "(start_latitude IS NULL) = (start_longitude IS NULL)",
        name="engineer_coordinate_pair",
    ),
    CheckConstraint(
        "start_address IS NOT NULL OR start_latitude IS NOT NULL",
        name="engineer_start_present",
    ),
)

engineer_schedules = Table(
    "engineer_schedules",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "engineer_id", ForeignKey("engineers.id", ondelete="CASCADE"), nullable=False
    ),
    Column("work_date", Date, nullable=False),
    Column("shift_start", Time, nullable=False),
    Column("shift_end", Time, nullable=False),
    UniqueConstraint("engineer_id", "work_date"),
    CheckConstraint("shift_start < shift_end", name="schedule_same_day"),
)

qualifications = Table(
    "qualifications",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    UniqueConstraint("project_id", "code"),
)

engineer_qualifications = Table(
    "engineer_qualifications",
    metadata_obj,
    Column(
        "engineer_id", ForeignKey("engineers.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "qualification_id",
        ForeignKey("qualifications.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

work_type_required_qualifications = Table(
    "work_type_required_qualifications",
    metadata_obj,
    Column(
        "work_type_id",
        ForeignKey("work_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "qualification_id",
        ForeignKey("qualifications.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

equipment_types = Table(
    "equipment_types",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    UniqueConstraint("project_id", "code"),
)

work_type_required_equipment = Table(
    "work_type_required_equipment",
    metadata_obj,
    Column(
        "work_type_id",
        ForeignKey("work_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "equipment_type_id",
        ForeignKey("equipment_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)

equipment_availability = Table(
    "equipment_availability",
    metadata_obj,
    Column(
        "project_id", ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "equipment_type_id",
        ForeignKey("equipment_types.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("availability_date", Date, primary_key=True),
    Column("available_units", Integer, nullable=False),
    CheckConstraint("available_units >= 0", name="equipment_units_nonnegative"),
)

planning_config = Table(
    "planning_config",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("sla_overdue_base", Integer, nullable=False, server_default="10000"),
    Column("sla_overdue_per_day", Integer, nullable=False, server_default="1000"),
    Column("sla_today", Integer, nullable=False, server_default="9000"),
    Column("sla_tomorrow", Integer, nullable=False, server_default="4000"),
    Column("sla_2_3_days", Integer, nullable=False, server_default="2000"),
    Column("sla_later", Integer, nullable=False, server_default="500"),
    Column("skill_one_engineer", Integer, nullable=False, server_default="500"),
    Column("skill_two_engineers", Integer, nullable=False, server_default="250"),
    Column("equipment_one_unit", Integer, nullable=False, server_default="500"),
    Column("equipment_two_units", Integer, nullable=False, server_default="250"),
    Column("window_30", Integer, nullable=False, server_default="300"),
    Column("window_60", Integer, nullable=False, server_default="150"),
    Column("window_120", Integer, nullable=False, server_default="50"),
    Column("travel_cost_per_minute", Integer, nullable=False, server_default="1"),
    Column("solver_time_limit_sec", Integer, nullable=False, server_default="30"),
    Column("max_jobs_per_run", Integer, nullable=False, server_default="1000"),
    Column(
        "travel_provider", String(32), nullable=False, server_default="VALHALLA_LOCAL"
    ),
    Column("travel_cache_ttl_days", Integer),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint("project_id", "version"),
)
Index(
    "uq_planning_config_active_project",
    planning_config.c.project_id,
    unique=True,
    postgresql_where=planning_config.c.active.is_(True),
)

planning_runs = Table(
    "planning_runs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("planning_date", Date, nullable=False),
    Column("timezone", String(64), nullable=False),
    Column("initiated_by_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("status", String(32), nullable=False),
    Column("solver_status", String(64)),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("objective", BigInteger),
    Column("drop_cost", BigInteger),
    Column("travel_cost", BigInteger),
    Column("input_jobs_count", Integer, nullable=False, server_default="0"),
    Column("eligible_jobs_count", Integer, nullable=False, server_default="0"),
    Column("assigned_jobs_count", Integer, nullable=False, server_default="0"),
    Column("unassigned_jobs_count", Integer, nullable=False, server_default="0"),
    Column("sla_critical_total", Integer, nullable=False, server_default="0"),
    Column("sla_critical_assigned", Integer, nullable=False, server_default="0"),
    Column("config_snapshot", JSONB, nullable=False, server_default="{}"),
    Column("input_snapshot", JSONB, nullable=False, server_default="{}"),
    Column("normalized_input_hash", String(64)),
    Column("travel_matrix_hash", String(64)),
    Column("traffic_reference_time", DateTime(timezone=True)),
    Column("solver_version", String(64)),
    Column("solver_time_ms", Integer),
    Column("error_code", String(64)),
    Column("error_message", Text),
    Column("validation_errors", JSONB, nullable=False, server_default="[]"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
Index(
    "uq_active_planning_run",
    planning_runs.c.project_id,
    planning_runs.c.planning_date,
    unique=True,
    postgresql_where=planning_runs.c.status.in_(("CREATED", "PREPARING", "RUNNING")),
)

planning_routes = Table(
    "planning_routes",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("planned_start", DateTime(timezone=True), nullable=False),
    Column("planned_finish", DateTime(timezone=True), nullable=False),
    Column("total_travel_min", Integer, nullable=False),
    Column("total_service_min", Integer, nullable=False),
    Column("total_waiting_min", Integer, nullable=False),
    UniqueConstraint("planning_run_id", "engineer_id"),
)

planning_route_jobs = Table(
    "planning_route_jobs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "planning_route_id",
        ForeignKey("planning_routes.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("planned_arrival", DateTime(timezone=True), nullable=False),
    Column("planned_start", DateTime(timezone=True), nullable=False),
    Column("planned_finish", DateTime(timezone=True), nullable=False),
    Column("travel_from_previous_min", Integer, nullable=False),
    Column("waiting_before_job_min", Integer, nullable=False),
    Column("drop_penalty_snapshot", Integer, nullable=False),
    UniqueConstraint("planning_route_id", "sequence"),
    UniqueConstraint("planning_run_id", "job_id"),
)

planning_equipment_assignments = Table(
    "planning_equipment_assignments",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("equipment_type_id", ForeignKey("equipment_types.id"), nullable=False),
    UniqueConstraint("planning_run_id", "engineer_id", "equipment_type_id"),
)

planning_unassigned_jobs = Table(
    "planning_unassigned_jobs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("drop_penalty", Integer, nullable=False),
    Column("primary_reason_code", String(64), nullable=False),
    Column("diagnostic_flags", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint("planning_run_id", "job_id"),
)

geocoding_cache = Table(
    "geocoding_cache",
    metadata_obj,
    Column("address_hash", String(64), primary_key=True),
    Column("normalized_address", Text, nullable=False),
    Column("latitude", Numeric(9, 6), nullable=False),
    Column("longitude", Numeric(9, 6), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("provider_version", String(64)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)

travel_time_cache = Table(
    "travel_time_cache",
    metadata_obj,
    Column("cache_key", String(64), primary_key=True),
    Column("origin_latitude", Numeric(9, 6), nullable=False),
    Column("origin_longitude", Numeric(9, 6), nullable=False),
    Column("destination_latitude", Numeric(9, 6), nullable=False),
    Column("destination_longitude", Numeric(9, 6), nullable=False),
    Column("profile", String(32), nullable=False),
    Column("duration_min", Integer),
    Column("provider", String(32), nullable=False),
    Column("provider_version", String(64)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
