from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from planning.domain.entities.engineer import Engineer
from planning.domain.entities.job import Job, UnassignedJob


@dataclass(frozen=True)
class PlanningConfig:
    id: int
    version: int
    sla_overdue_base: int
    sla_overdue_per_day: int
    sla_today: int
    sla_tomorrow: int
    sla_2_3_days: int
    sla_later: int
    skill_one_engineer: int
    skill_two_engineers: int
    equipment_one_unit: int
    equipment_two_units: int
    window_30: int
    window_60: int
    window_120: int
    travel_cost_per_minute: int
    solver_time_limit_sec: int
    max_jobs_per_run: int
    travel_provider: str
    future_opportunity_critical: int = 750
    future_opportunity_high: int = 500
    future_opportunity_limited: int = 250
    batch_initial_horizon_days: int = 7
    batch_maximum_horizon_days: int = 30
    batch_total_time_limit_sec: int = 900
    max_jobs_per_batch: int = 5000
    solver_seed: int = 1
    candidate_solver_time_limit_sec: int = 20
    single_cascade_time_limit_sec: int = 900
    event_time_limit_sec: int = 1200
    event_coalesce_window_sec: int = 30
    event_coalesce_max_wait_sec: int = 120
    max_parallel_candidate_models: int = 1
    distance_unit_meters: int = 10
    time_unit_seconds: int = 60
    travel_cache_ttl_days: int | None = None


@dataclass
class RouteJob:
    job_id: int
    sequence: int
    planned_arrival: datetime
    planned_start: datetime
    planned_finish: datetime
    travel_from_previous_min: int
    waiting_before_job_min: int
    drop_penalty: int
    distance_from_previous_meters: int = 0


@dataclass
class Route:
    engineer_id: int
    planned_start: datetime
    planned_finish: datetime
    total_travel_min: int
    total_service_min: int
    total_waiting_min: int
    jobs: list[RouteJob]
    equipment_type_ids: set[int]
    total_distance_meters: int = 0


@dataclass
class PlanningInput:
    project_id: int
    planning_date: date
    timezone: str
    config: PlanningConfig
    jobs: list[Job]
    engineers: list[Engineer]
    equipment_units: dict[int, int]
    pre_unassigned: list[UnassignedJob]
    input_jobs_count: int
    sla_critical_job_ids: frozenset[int]
    snapshot: dict[str, Any]
    preallocated_equipment_by_engineer: dict[int, frozenset[int]] = field(
        default_factory=dict
    )
    fixed_active_engineer_ids: frozenset[int] = frozenset()


@dataclass
class PlanningResult:
    routes: list[Route]
    unassigned: list[UnassignedJob]
    solver_status: str
    objective: int
    drop_cost: int
    travel_cost: int
    solver_time_ms: int
    validation_errors: list[str] = field(default_factory=list)
    travel_matrices: dict[str, list[list[int | None]]] = field(default_factory=dict)
    travel_time_seconds_matrices: dict[str, list[list[int | None]]] = field(
        default_factory=dict
    )
    distance_matrices: dict[str, list[list[int | None]]] = field(default_factory=dict)
    objective_metrics: dict[str, Any] = field(default_factory=dict)
