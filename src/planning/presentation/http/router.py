from datetime import date, datetime
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from planning.application.errors import PlanningError
from planning.application.service import PlanningService

planning_router = APIRouter(prefix="/api/projects", tags=["Planning"])


class StartPlanningRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planning_date: date
    timezone: str = Field(min_length=1, max_length=64)


class StartPlanningRunResponse(BaseModel):
    planning_run_id: int
    status: str
    solver_status: str
    metrics: dict[str, int]


class ErrorResponse(BaseModel):
    code: str
    message: str
    planning_run_id: int | None = None


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


ERROR_RESPONSES = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@planning_router.post(
    "/{project_id}/planning/runs",
    response_model=StartPlanningRunResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
@inject
async def start_run(
    project_id: int,
    body: StartPlanningRunRequest,
    service: FromDishka[PlanningService],
) -> dict[str, Any] | JSONResponse:
    try:
        return await service.start_run(project_id, body.planning_date, body.timezone)
    except PlanningError as error:
        return _error_response(error)


@planning_router.get(
    "/{project_id}/planning/runs/{run_id}",
    response_model=PlanningRunDetailsResponse,
    responses=ERROR_RESPONSES,
)
@inject
async def get_run(
    project_id: int,
    run_id: int,
    service: FromDishka[PlanningService],
) -> Any:
    try:
        return await service.get_run(project_id, run_id)
    except PlanningError as error:
        return _error_response(error)


@planning_router.get(
    "/{project_id}/planning/runs",
    response_model=list[PlanningRunRecord],
    responses=ERROR_RESPONSES,
)
@inject
async def list_runs(
    project_id: int,
    service: FromDishka[PlanningService],
    planning_date: date | None = None,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    try:
        return await service.list_runs(
            project_id, planning_date, run_status, limit, offset
        )
    except PlanningError as error:
        return _error_response(error)


def _error_response(error: PlanningError) -> JSONResponse:
    content: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.run_id is not None:
        content["planning_run_id"] = error.run_id
    return JSONResponse(content=content, status_code=error.http_status)
