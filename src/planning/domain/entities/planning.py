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
