from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from auth.infrastructure.persistence_sqla.orm_registry import metadata_obj

projects = Table(
    "projects",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("internal_code", String(64), nullable=False, unique=True),
    Column("planning_timezone", String(64), nullable=False),
    Column("planning_one_day_enabled", Boolean, nullable=False, server_default="true"),
    Column("status", String(16), nullable=False, server_default="ACTIVE"),
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
)
Index(
    "uq_projects_active_name_ci",
    func.lower(projects.c.name),
    unique=True,
    postgresql_where=projects.c.status == "ACTIVE",
)

# Dispatcher authorization is intentionally independent from ``users.project_id``.
# That column remains the engineer scope and a compatibility mirror only when a
# dispatcher has exactly one project. Multi-project access lives in this table.
dispatcher_projects = Table(
    "dispatcher_projects",
    metadata_obj,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "project_id",
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "assigned_by",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
    ),
    Column(
        "assigned_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
Index("ix_dispatcher_projects_project_id", dispatcher_projects.c.project_id)

work_types = Table(
    "work_types",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("code", String(64), nullable=False),
    Column("name", String(255), nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("priority", String(16), nullable=False, server_default="LOW"),
    Column("default_service_duration_min", Integer),
    Column("required_transport", String(16)),
    UniqueConstraint("project_id", "code"),
    CheckConstraint(
        "default_service_duration_min IS NULL OR default_service_duration_min > 0",
        name="work_type_duration_positive",
    ),
    CheckConstraint(
        "priority IN ('CRITICAL','HIGH','MEDIUM','LOW')",
        name="work_type_priority_valid",
    ),
    CheckConstraint(
        "required_transport IS NULL OR "
        "required_transport IN ('CAR','NONE','BICYCLE')",
        name="transport_valid",
    ),
)
Index(
    "uq_work_types_project_name_ci",
    work_types.c.project_id,
    func.lower(work_types.c.name),
    unique=True,
)

jobs = Table(
    "jobs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(255)),
    Column("internal_code", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("cancelled_by_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("previous_status", String(32)),
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
    Column("import_batch_id", ForeignKey("job_import_batches.id")),
    Column("import_row_number", Integer),
    Column(
        "received_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("ingest_sequence", Integer, nullable=False, server_default="1"),
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
    UniqueConstraint("project_id", "external_id", name="uq_jobs_project_id"),
    UniqueConstraint(
        "import_batch_id", "import_row_number", name="uq_jobs_import_origin"
    ),
    UniqueConstraint(
        "project_id", "internal_code", name="uq_jobs_project_internal_code"
    ),
    CheckConstraint(
        "service_duration_min IS NULL OR service_duration_min > 0",
        name="job_duration_positive",
    ),
    CheckConstraint("ingest_sequence > 0", name="job_ingest_sequence_positive"),
    CheckConstraint(
        "(latitude IS NULL) = (longitude IS NULL)", name="job_coordinate_pair"
    ),
)

job_import_batches = Table(
    "job_import_batches",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("created_by", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("original_filename", String(255), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("source_bytes", LargeBinary),
    Column("source_encoding", String(16)),
    Column("source_delimiter", String(1)),
    Column("validation_rules_version", String(32)),
    Column("address_provider_version", String(32)),
    Column("normalized_payload_sha256", String(64)),
    Column("status", String(32), nullable=False, server_default="UPLOADED"),
    Column("stage", String(64)),
    Column("total_rows", Integer, nullable=False, server_default="0"),
    Column("processed_rows", Integer, nullable=False, server_default="0"),
    Column("error_count", Integer, nullable=False, server_default="0"),
    Column("warning_count", Integer, nullable=False, server_default="0"),
    Column("created_count", Integer, nullable=False, server_default="0"),
    Column("warnings_acknowledged_by", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("warnings_acknowledged_at", DateTime(timezone=True)),
    Column("planning_event_id", BigInteger),
    Column("apply_idempotency_key", String(64)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("validation_started_at", DateTime(timezone=True)),
    Column("validation_finished_at", DateTime(timezone=True)),
    Column("applied_at", DateTime(timezone=True)),
    Column("source_file_expires_at", DateTime(timezone=True), nullable=False),
    Column("technical_error_category", String(64)),
    Column("package_issues", JSONB, nullable=False, server_default="[]"),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
)
Index(
    "uq_job_import_live_hash",
    job_import_batches.c.project_id,
    job_import_batches.c.content_sha256,
    unique=True,
    postgresql_where=job_import_batches.c.status != "EXPIRED",
)

job_import_rows = Table(
    "job_import_rows",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "batch_id",
        ForeignKey("job_import_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("row_number", Integer, nullable=False),
    Column("raw_required_values_json", JSONB, nullable=False),
    Column("normalized_address", Text),
    Column("canonical_address_key", String(255)),
    Column("latitude", Numeric(9, 6)),
    Column("longitude", Numeric(9, 6)),
    Column("sla_date", Date),
    Column("time_window_start", Time),
    Column("time_window_end", Time),
    Column("work_type_id", ForeignKey("work_types.id")),
    Column("work_type_name", String(255)),
    Column("work_type_duration_min", Integer),
    Column("severity", String(16)),
    Column("issues_json", JSONB, nullable=False, server_default="[]"),
    Column("created_job_id", ForeignKey("jobs.id")),
    UniqueConstraint("batch_id", "row_number"),
)
Index(
    "ix_job_import_rows_batch", job_import_rows.c.batch_id, job_import_rows.c.row_number
)

job_import_audit = Table(
    "job_import_audit",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "batch_id",
        ForeignKey("job_import_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("actor_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("event_type", String(64), nullable=False),
    Column("details", JSONB, nullable=False, server_default="{}"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
Index("ix_job_import_audit_batch", job_import_audit.c.batch_id, job_import_audit.c.id)

engineers = Table(
    "engineers",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(255), nullable=False),
    Column("internal_code", String(64), nullable=False),
    Column("active", Boolean, nullable=False, server_default="true"),
    Column("transport_type", String(16), nullable=False),
    CheckConstraint(
        "transport_type IN ('CAR','NONE','BICYCLE')", name="transport_valid"
    ),
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
    UniqueConstraint(
        "project_id", "internal_code", name="uq_engineers_project_internal_code"
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
    Column("active", Boolean, nullable=False, server_default="true"),
    UniqueConstraint("project_id", "code"),
)
Index(
    "uq_qualifications_project_name_ci",
    qualifications.c.project_id,
    func.lower(qualifications.c.name),
    unique=True,
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
    Column("available_units", Integer, nullable=False, server_default="0"),
    Column("active", Boolean, nullable=False, server_default="true"),
    UniqueConstraint("project_id", "code"),
    CheckConstraint("available_units >= 0", name="equipment_type_units_nonnegative"),
)
Index(
    "uq_equipment_types_project_name_ci",
    equipment_types.c.project_id,
    func.lower(equipment_types.c.name),
    unique=True,
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
    Column("solver_time_limit_sec", Integer, nullable=False, server_default="60"),
    Column("max_jobs_per_run", Integer, nullable=False, server_default="1000"),
    Column(
        "travel_provider", String(32), nullable=False, server_default="VALHALLA_LOCAL"
    ),
    Column("travel_cache_ttl_days", Integer),
    Column(
        "future_opportunity_critical", Integer, nullable=False, server_default="750"
    ),
    Column("future_opportunity_high", Integer, nullable=False, server_default="500"),
    Column("future_opportunity_limited", Integer, nullable=False, server_default="250"),
    Column("batch_initial_horizon_days", Integer, nullable=False, server_default="7"),
    Column("batch_maximum_horizon_days", Integer, nullable=False, server_default="30"),
    Column("batch_total_time_limit_sec", Integer, nullable=False, server_default="900"),
    Column("max_jobs_per_batch", Integer, nullable=False, server_default="5000"),
    Column("solver_seed", Integer, nullable=False, server_default="1"),
    Column(
        "candidate_solver_time_limit_sec",
        Integer,
        nullable=False,
        server_default="20",
    ),
    Column(
        "single_cascade_time_limit_sec",
        Integer,
        nullable=False,
        server_default="900",
    ),
    Column("event_time_limit_sec", Integer, nullable=False, server_default="1200"),
    Column(
        "event_coalesce_window_sec",
        Integer,
        nullable=False,
        server_default="30",
    ),
    Column(
        "event_coalesce_max_wait_sec",
        Integer,
        nullable=False,
        server_default="120",
    ),
    Column(
        "max_parallel_candidate_models",
        Integer,
        nullable=False,
        server_default="1",
    ),
    Column("nightly_planning_enabled", Boolean, nullable=False, server_default="true"),
    Column("nightly_planning_time", Time, nullable=False, server_default="02:00:00"),
    Column("distance_unit_meters", Integer, nullable=False, server_default="10"),
    Column("time_unit_seconds", Integer, nullable=False, server_default="60"),
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
    Column("planning_batch_id", BigInteger),
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
    Column("travel_snapshot", JSONB, nullable=False, server_default="[]"),
    Column("traffic_reference_time", DateTime(timezone=True)),
    Column("solver_version", String(64)),
    Column("solver_time_ms", Integer),
    Column("objective_range_snapshot", JSONB, nullable=False, server_default="{}"),
    Column("fixed_active_engineer_count", Integer, nullable=False, server_default="0"),
    Column(
        "newly_activated_engineer_count", Integer, nullable=False, server_default="0"
    ),
    Column("used_engineer_count", Integer, nullable=False, server_default="0"),
    Column("total_distance_meters", BigInteger, nullable=False, server_default="0"),
    Column(
        "max_engineer_distance_meters", BigInteger, nullable=False, server_default="0"
    ),
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

planning_batches = Table(
    "planning_batches",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("requested_start_date", Date, nullable=False),
    Column("effective_start_date", Date, nullable=False),
    Column("initial_horizon_end", Date, nullable=False),
    Column("maximum_horizon_end", Date, nullable=False),
    Column("processed_through_date", Date),
    Column("status", String(32), nullable=False),
    Column("completion_reason", String(64)),
    Column(
        "initiated_by_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    ),
    Column("idempotency_key", String(255), nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("configuration_version", String(64), nullable=False),
    Column("input_snapshot", JSONB, nullable=False),
    Column("stop_requested_at", DateTime(timezone=True)),
    Column("current_flag", Boolean, nullable=False, server_default="false"),
    Column("stale_for_publication", Boolean, nullable=False, server_default="false"),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    Column("error_code", String(64)),
    Column("error_message", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint(
        "project_id",
        "idempotency_key",
        name="uq_planning_batches_project_id",
    ),
)
Index(
    "uq_active_planning_batch_project",
    planning_batches.c.project_id,
    unique=True,
    postgresql_where=planning_batches.c.status.in_(
        ("CREATED", "PREPARING", "RUNNING", "STOP_REQUESTED")
    ),
)

# Dynamic planning events are persisted even though execution remains in-process.
# They are the durable hand-off between a committed business write and a planning
# task, and also provide the user-visible audit trail required by the API.
planning_events = Table(
    "planning_events",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("job_ids", JSONB, nullable=False, server_default="[]"),
    Column("engineer_ids", JSONB, nullable=False, server_default="[]"),
    Column("event_payload", JSONB, nullable=False, server_default="{}"),
    Column("initiator", String(16), nullable=False),
    Column("actor_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("idempotency_key", String(255)),
    Column("state", String(32), nullable=False, server_default="PENDING"),
    Column("planning_batch_id", ForeignKey("planning_batches.id", ondelete="SET NULL")),
    Column(
        "published_plan_version_id",
        BigInteger,
        ForeignKey("project_plan_versions.id", use_alter=True),
    ),
    Column("input_hash", String(64)),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
    Column("candidate_total", Integer, nullable=False, server_default="0"),
    Column("candidate_completed", Integer, nullable=False, server_default="0"),
    Column("current_candidate_engineer_id", ForeignKey("engineers.id")),
    Column("error_code", String(64)),
    Column("error_message", Text),
    Column(
        "requested_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    UniqueConstraint(
        "project_id",
        "idempotency_key",
        name="uq_planning_events_idempotency",
    ),
)
Index(
    "ix_planning_events_pending", planning_events.c.project_id, planning_events.c.state
)

candidate_evaluations = Table(
    "candidate_evaluations",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "planning_event_id",
        ForeignKey("planning_events.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("planning_batch_id", ForeignKey("planning_batches.id", ondelete="CASCADE")),
    Column("subject_job_id", ForeignKey("jobs.id"), nullable=False),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("planning_date", Date, nullable=False),
    Column("solver_status", String(64), nullable=False),
    Column("validator_status", String(32), nullable=False),
    Column("rejection_reason", String(128)),
    Column("original_route", JSONB, nullable=False, server_default="[]"),
    Column("result_route", JSONB, nullable=False, server_default="[]"),
    Column("dropped_job_ids", JSONB, nullable=False, server_default="[]"),
    Column("score_vector", JSONB),
    Column("travel_matrix_hash", String(64)),
    Column("selected", Boolean, nullable=False, server_default="false"),
    Column("solver_time_ms", Integer, nullable=False, server_default="0"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "project_id",
        "planning_event_id",
        "subject_job_id",
        "engineer_id",
        name="uq_candidate_evaluation_event_job_engineer",
    ),
)
Index(
    "uq_current_planning_batch_project",
    planning_batches.c.project_id,
    unique=True,
    postgresql_where=planning_batches.c.current_flag.is_(True),
)

planning_batch_days = Table(
    "planning_batch_days",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "planning_batch_id",
        ForeignKey("planning_batches.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("planning_date", Date, nullable=False),
    Column("block_number", Integer, nullable=False),
    Column("status", String(32), nullable=False),
    Column("planning_run_id", ForeignKey("planning_runs.id")),
    Column("input_jobs_count", Integer, nullable=False, server_default="0"),
    Column("solver_candidates_count", Integer, nullable=False, server_default="0"),
    Column("assigned_count", Integer, nullable=False, server_default="0"),
    Column("unassigned_count", Integer, nullable=False, server_default="0"),
    Column("deferred_count", Integer, nullable=False, server_default="0"),
    Column("dropped_count", Integer, nullable=False, server_default="0"),
    Column("input_job_ids_hash", String(64)),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("error_code", String(64)),
    UniqueConstraint("planning_batch_id", "planning_date"),
)

planning_batch_jobs = Table(
    "planning_batch_jobs",
    metadata_obj,
    Column(
        "planning_batch_id",
        ForeignKey("planning_batches.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("job_id", ForeignKey("jobs.id"), primary_key=True),
    Column("snapshot_sla_date", Date, nullable=False),
    Column("eligibility_status", String(32), nullable=False),
    Column("priority_group", String(64)),
    Column("future_opportunity_count", Integer),
    Column("future_opportunity_rank", Integer),
    Column("future_opportunity_bonus", Integer),
    Column("daily_drop_penalty", BigInteger),
    Column("cascade_drop_penalty", BigInteger),
    Column("priority_bonus", BigInteger, nullable=False, server_default="0"),
    Column("solver_drop_cost", BigInteger),
    Column("processing_status", String(40), nullable=False),
    Column("assigned_date", Date),
    Column("planning_run_id", ForeignKey("planning_runs.id")),
    Column("primary_reason_code", String(64)),
    Column("diagnostic_flags", JSONB, nullable=False, server_default="{}"),
    Column("last_considered_date", Date),
    Column("last_planning_run_id", ForeignKey("planning_runs.id")),
    Column("attempt_count", Integer, nullable=False, server_default="0"),
)

# The circular reference is declared after both tables exist in metadata.
planning_runs.append_constraint(
    ForeignKeyConstraint(
        [planning_runs.c.planning_batch_id],
        [planning_batches.c.id],
        ondelete="CASCADE",
        name="fk_planning_runs_planning_batch_id_planning_batches",
    )
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
    Column("total_distance_meters", BigInteger, nullable=False, server_default="0"),
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
    Column(
        "distance_from_previous_meters", BigInteger, nullable=False, server_default="0"
    ),
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

daily_plans = Table(
    "daily_plans",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("planning_date", Date, nullable=False),
    Column(
        "current_version_id",
        BigInteger,
        ForeignKey(
            "plan_versions.id", use_alter=True, name="fk_daily_plans_current_version"
        ),
    ),
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
    UniqueConstraint("project_id", "planning_date", name="uq_daily_plans_project_date"),
)

plan_versions = Table(
    "plan_versions",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "daily_plan_id",
        ForeignKey("daily_plans.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("version_number", Integer, nullable=False),
    Column(
        "planning_run_id", ForeignKey("planning_runs.id"), nullable=False, unique=True
    ),
    Column("status", String(32), nullable=False, server_default="PUBLISHED"),
    Column("published_by", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column(
        "published_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("superseded_at", DateTime(timezone=True)),
    UniqueConstraint("daily_plan_id", "version_number", name="uq_plan_versions_number"),
)

assignments = Table(
    "assignments",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "plan_version_id",
        ForeignKey("plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("planned_start", DateTime(timezone=True), nullable=False),
    Column("planned_finish", DateTime(timezone=True), nullable=False),
    Column("route_data", JSONB, nullable=False, server_default="{}"),
    Column("requirement_snapshot", JSONB, nullable=False, server_default="{}"),
    Column("active", Boolean, nullable=False, server_default="true"),
    UniqueConstraint("plan_version_id", "job_id", name="uq_assignments_version_job"),
    UniqueConstraint(
        "plan_version_id",
        "engineer_id",
        "sequence",
        name="uq_assignments_version_engineer_sequence",
    ),
)

# A published dynamic plan is versioned for the whole project/horizon.  The
# legacy daily_plans/plan_versions tables remain readable for pre-migration
# audit, but all new dynamic publications use these tables atomically.
project_plan_versions = Table(
    "project_plan_versions",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("planning_batch_id", ForeignKey("planning_batches.id", ondelete="SET NULL")),
    Column("planning_event_id", ForeignKey("planning_events.id", ondelete="SET NULL")),
    Column("input_hash", String(64), nullable=False),
    Column("trigger_source", String(32), nullable=False),
    Column("actor_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("is_current", Boolean, nullable=False, server_default="false"),
    Column("unassigned_jobs", JSONB, nullable=False, server_default="[]"),
    Column("metrics", JSONB, nullable=False, server_default="{}"),
    Column(
        "published_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("superseded_at", DateTime(timezone=True)),
    UniqueConstraint(
        "project_id",
        "version_number",
        name="uq_project_plan_version_number",
    ),
)
Index(
    "uq_project_plan_versions_current",
    project_plan_versions.c.project_id,
    unique=True,
    postgresql_where=project_plan_versions.c.is_current.is_(True),
)

planning_day_results = Table(
    "planning_day_results",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "plan_version_id",
        ForeignKey("project_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("planning_date", Date, nullable=False),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("input_job_ids_hash", String(64)),
    Column("input_jobs_count", Integer, nullable=False, server_default="0"),
    Column("assigned_count", Integer, nullable=False, server_default="0"),
    Column("unassigned_count", Integer, nullable=False, server_default="0"),
    Column("solver_status", String(64)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "plan_version_id",
        "planning_date",
        name="uq_planning_day_results_version_date",
    ),
)
Index(
    "ix_planning_day_results_project_date",
    planning_day_results.c.project_id,
    planning_day_results.c.planning_date,
)

# The comparison is stored against the immutable planning run, not recomputed
# from mutable jobs or engineers.  The complete baseline payload keeps the
# deterministic FIFO order, route legs and diagnostics available for audit.
planning_baseline_results = Table(
    "planning_baseline_results",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "plan_version_id",
        ForeignKey("project_plan_versions.id", ondelete="SET NULL"),
    ),
    Column("planning_date", Date, nullable=False),
    Column("status", String(48), nullable=False),
    Column("algorithm_version", String(32), nullable=False),
    Column(
        "snapshot_version", String(32), nullable=False, server_default="DAY_INPUT_V1"
    ),
    Column("input_hash", String(64), nullable=False),
    Column("travel_matrix_hash", String(64)),
    Column("result_hash", String(64)),
    Column("input_jobs_count", Integer, nullable=False, server_default="0"),
    Column("assigned_jobs_count", Integer, nullable=False, server_default="0"),
    Column("unassigned_jobs_count", Integer, nullable=False, server_default="0"),
    Column("window_hit_count", Integer, nullable=False, server_default="0"),
    Column("window_miss_count", Integer, nullable=False, server_default="0"),
    Column("window_hit_rate", Numeric(7, 4)),
    Column("active_engineer_count", Integer, nullable=False, server_default="0"),
    Column("total_distance_meters", BigInteger, nullable=False, server_default="0"),
    Column("earliest_shift_start", DateTime(timezone=True)),
    Column("calculation_time_ms", Integer, nullable=False, server_default="0"),
    Column("failure_code", String(64)),
    Column("failure_message", Text),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, server_default="1"),
    Column("next_retry_at", DateTime(timezone=True)),
    Column("last_attempt_at", DateTime(timezone=True)),
    Column("result_payload", JSONB, nullable=False, server_default="{}"),
    Column("distance_matrices", JSONB, nullable=False, server_default="{}"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "project_id",
        "planning_run_id",
        "algorithm_version",
        name="uq_planning_baseline_run_algorithm",
    ),
)
Index(
    "ix_planning_baseline_project_date",
    planning_baseline_results.c.project_id,
    planning_baseline_results.c.planning_date,
)

planning_baseline_routes = Table(
    "planning_baseline_routes",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "baseline_result_id",
        ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("engineer_input_order", Integer, nullable=False),
    Column("shift_start", DateTime(timezone=True), nullable=False),
    Column("shift_end", DateTime(timezone=True), nullable=False),
    Column("capacity_minutes", Integer, nullable=False),
    Column("assigned_jobs_count", Integer, nullable=False),
    Column("consumed_minutes", Integer, nullable=False),
    Column("remaining_minutes", Integer, nullable=False),
    Column("distance_meters", BigInteger, nullable=False),
    Column("start_location_snapshot", JSONB, nullable=False),
    UniqueConstraint(
        "baseline_result_id",
        "engineer_id",
        name="uq_planning_baseline_route_engineer",
    ),
)

planning_baseline_route_jobs = Table(
    "planning_baseline_route_jobs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "baseline_result_id",
        ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "baseline_route_id",
        ForeignKey("planning_baseline_routes.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("job_input_order", Integer, nullable=False),
    Column("route_position", Integer, nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("ingest_sequence", Integer, nullable=False),
    Column("service_duration_minutes", Integer, nullable=False),
    Column("standard_travel_minutes", Integer, nullable=False, server_default="20"),
    Column("planned_start", DateTime(timezone=True), nullable=False),
    Column("planned_finish", DateTime(timezone=True), nullable=False),
    Column("window_from_min", Integer, nullable=False),
    Column("window_to_min", Integer, nullable=False),
    Column("window_hit", Boolean, nullable=False),
    Column("previous_location_type", String(24), nullable=False),
    Column("distance_from_previous_meters", BigInteger, nullable=False),
    UniqueConstraint(
        "baseline_result_id",
        "job_id",
        name="uq_planning_baseline_result_job",
    ),
    UniqueConstraint(
        "baseline_route_id",
        "route_position",
        name="uq_planning_baseline_route_position",
    ),
)

planning_baseline_unassigned_jobs = Table(
    "planning_baseline_unassigned_jobs",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "baseline_result_id",
        ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("job_input_order", Integer, nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("diagnostics", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint(
        "baseline_result_id",
        "job_id",
        name="uq_planning_baseline_unassigned_job",
    ),
)

planning_plan_comparisons = Table(
    "planning_plan_comparisons",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "planning_run_id",
        ForeignKey("planning_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "baseline_result_id",
        ForeignKey("planning_baseline_results.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("formula_version", String(64), nullable=False),
    Column("coverage_comparable", Boolean, nullable=False),
    Column("baseline_assigned_job_ids_hash", String(64), nullable=False),
    Column("optimized_assigned_job_ids_hash", String(64), nullable=False),
    Column("baseline_metrics", JSONB, nullable=False),
    Column("optimized_metrics", JSONB, nullable=False),
    Column("deltas", JSONB, nullable=False),
    Column("engineer_metrics", JSONB, nullable=False, server_default="[]"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "planning_run_id",
        "formula_version",
        name="uq_planning_comparison_run_formula",
    ),
)
Index(
    "ix_planning_comparisons_project_run",
    planning_plan_comparisons.c.project_id,
    planning_plan_comparisons.c.planning_run_id,
)

planning_cancelled_job_snapshots = Table(
    "planning_cancelled_job_snapshots",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "plan_version_id",
        ForeignKey("project_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("planning_date", Date, nullable=False),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("previous_sequence", Integer),
    Column("planned_start", DateTime(timezone=True)),
    Column("planned_finish", DateTime(timezone=True)),
    Column("cancelled_at", DateTime(timezone=True), nullable=False),
    Column("cancelled_by_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column("cancelled_by_username", String(255)),
    Column("job_snapshot", JSONB, nullable=False, server_default="{}"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    UniqueConstraint(
        "plan_version_id",
        "job_id",
        name="uq_planning_cancelled_snapshot_version_job",
    ),
)
Index(
    "ix_planning_cancelled_snapshots_version_date",
    planning_cancelled_job_snapshots.c.plan_version_id,
    planning_cancelled_job_snapshots.c.planning_date,
)

project_plan_assignments = Table(
    "project_plan_assignments",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "plan_version_id",
        ForeignKey("project_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("planning_date", Date, nullable=False),
    Column("engineer_id", ForeignKey("engineers.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("planned_arrival", DateTime(timezone=True), nullable=False),
    Column("planned_start", DateTime(timezone=True), nullable=False),
    Column("planned_finish", DateTime(timezone=True), nullable=False),
    Column("travel_from_previous_min", Integer, nullable=False, server_default="0"),
    Column("waiting_before_job_min", Integer, nullable=False, server_default="0"),
    Column(
        "distance_from_previous_meters", BigInteger, nullable=False, server_default="0"
    ),
    Column("requirement_snapshot", JSONB, nullable=False, server_default="{}"),
    UniqueConstraint(
        "project_id",
        "plan_version_id",
        "job_id",
        name="uq_project_plan_assignment_job",
    ),
    UniqueConstraint(
        "project_id",
        "plan_version_id",
        "planning_date",
        "engineer_id",
        "sequence",
        name="uq_project_plan_assignment_sequence",
    ),
)
Index(
    "ix_project_plan_assignments_project_job",
    project_plan_assignments.c.project_id,
    project_plan_assignments.c.job_id,
)

plan_changes = Table(
    "plan_changes",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column(
        "plan_version_id",
        ForeignKey("project_plan_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", ForeignKey("jobs.id"), nullable=False),
    Column("change_type", String(32), nullable=False),
    Column("old_assignment", JSONB),
    Column("new_assignment", JSONB),
    Column("reason", String(128), nullable=False),
)
Index("ix_plan_changes_version", plan_changes.c.plan_version_id)

job_status_history = Table(
    "job_status_history",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("job_id", ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
    Column("assignment_id", ForeignKey("assignments.id")),
    Column(
        "project_plan_assignment_id",
        ForeignKey(
            "project_plan_assignments.id",
            name="fk_job_history_project_assignment",
        ),
    ),
    Column("old_status", String(32), nullable=False),
    Column("new_status", String(32), nullable=False),
    Column("actor_user_id", UUID(as_uuid=True), ForeignKey("users.id"), nullable=False),
    Column("reason", Text),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
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
    Column("travel_time_seconds", Integer),
    Column("distance_meters", BigInteger),
    Column("provider", String(32), nullable=False),
    Column("provider_version", String(64)),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)

job_planning_state = Table(
    "job_planning_state",
    metadata_obj,
    Column("job_id", ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column("state", String(16), nullable=False),
    Column("reason_code", String(64)),
    Column(
        "changed_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
    Column("source_event_id", ForeignKey("planning_events.id", ondelete="SET NULL")),
    Column(
        "plan_version_id", ForeignKey("project_plan_versions.id", ondelete="SET NULL")
    ),
)

engineer_availability_events = Table(
    "engineer_availability_events",
    metadata_obj,
    Column("id", BigInteger, primary_key=True),
    Column("project_id", ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
    Column(
        "engineer_id", ForeignKey("engineers.id", ondelete="CASCADE"), nullable=False
    ),
    Column("change_type", String(32), nullable=False),
    Column("effective_date", Date, nullable=False),
    Column("old_interval", JSONB),
    Column("new_interval", JSONB),
    Column("actor_user_id", UUID(as_uuid=True), ForeignKey("users.id")),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    ),
)
