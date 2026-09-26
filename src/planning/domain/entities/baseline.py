from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class BaselineRouteJob:
    job_id: int
    job_input_order: int
    route_position: int
    received_at: datetime
    ingest_sequence: int
    service_duration_minutes: int
    planned_start: datetime
    planned_finish: datetime
    window_from_min: int
    window_to_min: int
    window_hit: bool
    previous_location_type: str
    distance_from_previous_meters: int


@dataclass(frozen=True)
class BaselineRoute:
    engineer_id: int
    engineer_name: str | None
    engineer_input_order: int
    shift_start: datetime
    shift_end: datetime
    capacity_minutes: int
    consumed_minutes: int
    remaining_minutes: int
    distance_meters: int
    jobs: tuple[BaselineRouteJob, ...]
    start_location_snapshot: dict[str, float]


@dataclass(frozen=True)
class BaselineUnassignedJob:
    job_id: int
    job_input_order: int
    reason_code: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BaselinePlanResult:
    project_id: int
    planning_date: date
    status: str
    algorithm_version: str
    input_hash: str
    travel_matrix_hash: str | None
    input_jobs_count: int
    assigned_jobs_count: int
    unassigned_jobs_count: int
    window_hit_count: int
    window_miss_count: int
    window_hit_rate: float | None
    active_engineer_count: int
    total_distance_meters: int
    routes: tuple[BaselineRoute, ...] = ()
    unassigned: tuple[BaselineUnassignedJob, ...] = ()
    result_hash: str | None = None
    calculation_time_ms: int = 0
    earliest_shift_start: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class BaselineComparison:
    coverage_comparable: bool
    baseline_assigned_job_ids_hash: str
    optimized_assigned_job_ids_hash: str
    baseline_metrics: dict[str, Any]
    optimized_metrics: dict[str, Any]
    deltas: dict[str, Any]
    engineer_metrics: tuple[dict[str, Any], ...]
    formula_version: str = "BASELINE_COMPARISON_V1"
