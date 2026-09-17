from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StartPlanningRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planning_date: date
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


class StartPlanningRunResponse(BaseModel):
    planning_event_id: int
    planning_run_id: int | None = None
    status: str
    status_url: str


class PlanningRunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    project_id: int
    planning_date: date
    timezone: str
    status: str
    solver_status: str | None = None
    objective: int | None = None
    drop_cost: int | None = None
    travel_cost: int | None = None
    assigned_jobs_count: int
    unassigned_jobs_count: int
    created_at: datetime


class RouteJobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: int
    sequence: int
    planned_arrival: datetime
    planned_start: datetime
    planned_finish: datetime
    travel_from_previous_min: int
    waiting_before_job_min: int


class RouteResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    engineer_id: int
    planned_start: datetime
    planned_finish: datetime
    total_travel_min: int
    total_service_min: int
    total_waiting_min: int
    jobs: list[RouteJobResponse]
    equipment_type_ids: list[int]


class UnassignedJobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: int
    drop_penalty: int
    primary_reason_code: str
    diagnostic_flags: dict[str, Any]


class PlanningRunDetailsResponse(BaseModel):
    run: PlanningRunRecord
    routes: list[RouteResponse]
    unassigned_jobs: list[UnassignedJobResponse]
