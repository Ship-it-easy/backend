from datetime import date, datetime, time
from typing import Annotated, Any

import httpx

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from planning.application.errors import PlanningError
from planning.application.interfaces import JobsRepository
from planning.application.service import PlanningService
from auth.entrypoint.config import PlanningServiceConfig
from planning.infrastructure.persistence.tables import equipment_types, qualifications, work_types

planning_router = APIRouter(prefix="/api/projects", tags=["Planning"])
geocoding_router = APIRouter(prefix="/api/geocoding", tags=["Geocoding"])


class GeocodingCandidate(BaseModel):
    display_name: str
    latitude: float
    longitude: float


@geocoding_router.get("/search", response_model=list[GeocodingCandidate])
@inject
async def search_addresses(
    q: Annotated[str, Query(min_length=3, max_length=300)],
    config: FromDishka[PlanningServiceConfig],
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=config.nominatim_url, timeout=config.geoservice_timeout_sec) as client:
        response = await client.get("/search", params={"q": q, "format": "jsonv2", "limit": 5, "countrycodes": "ru", "viewbox": config.nominatim_viewbox, "bounded": 1})
        response.raise_for_status()
    return [{"display_name": item["display_name"], "latitude": float(item["lat"]), "longitude": float(item["lon"])} for item in response.json()]


class StartPlanningRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planning_date: date
    timezone: str = Field(min_length=1, max_length=64)


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    planning_timezone: str = Field(default="Asia/Yekaterinburg", min_length=1, max_length=64)


class ProjectResponse(BaseModel):
    id: int
    name: str
    planning_timezone: str
    planning_one_day_enabled: bool


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


class CreateJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str | None = Field(default=None, max_length=255)
    address: str = Field(min_length=1)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    sla_date: date
    work_type_id: int
    service_duration_min: int | None = Field(default=None, gt=0)
    time_window_start: time | None = None
    time_window_end: time | None = None

    @model_validator(mode="after")
    def validate_time_window(self) -> "CreateJobRequest":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if self.time_window_start is not None and self.time_window_end is not None:
            if self.time_window_start >= self.time_window_end:
                raise ValueError(
                    "time_window_start must be earlier than time_window_end"
                )
        return self


class BulkCreateJobsRequest(BaseModel):
    jobs: list[CreateJobRequest] = Field(min_length=1, max_length=1000)


class JobResponse(BaseModel):
    id: int
    project_id: int
    external_id: str | None
    status: str
    address: str
    latitude: float | None
    longitude: float | None
    sla_date: date
    time_window_start: time | None
    time_window_end: time | None
    work_type_id: int
    service_duration_min: int | None
    created_at: datetime
    updated_at: datetime


class CreateEngineerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    active: bool = True
    transport_type: str = Field(pattern="^(CAR|NONE)$")
    start_address: str | None = None
    start_latitude: float | None = Field(default=None, ge=-90, le=90)
    start_longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def validate_start(self) -> "CreateEngineerRequest":
        if self.start_address is None and self.start_latitude is None:
            raise ValueError("start_address or coordinates are required")
        if (self.start_latitude is None) != (self.start_longitude is None):
            raise ValueError("start_latitude and start_longitude must be provided together")
        return self


class EngineerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    project_id: int
    name: str
    active: bool
    transport_type: str
    start_address: str | None
    start_latitude: float | None
    start_longitude: float | None
    created_at: datetime
    updated_at: datetime


class CreateScheduleRequest(BaseModel):
    work_date: date
    shift_start: time
    shift_end: time

    @model_validator(mode="after")
    def validate_shift(self) -> "CreateScheduleRequest":
        if self.shift_start >= self.shift_end:
            raise ValueError("shift_start must be earlier than shift_end")
        return self


class ScheduleResponse(BaseModel):
    id: int
    engineer_id: int
    work_date: date
    shift_start: time
    shift_end: time


class EquipmentAvailabilityRequest(BaseModel):
    equipment_type_id: int
    availability_date: date
    available_units: int = Field(ge=0)


class EquipmentAvailabilityResponse(EquipmentAvailabilityRequest):
    project_id: int


class CatalogRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    active: bool = True
    default_service_duration_min: int | None = Field(default=None, gt=0)
    required_transport: str | None = Field(default=None, pattern="^(CAR|NONE)$")


class CatalogResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int
    project_id: int
    code: str
    name: str


class EngineerQualificationsRequest(BaseModel):
    qualification_ids: list[int] = Field(default_factory=list, max_length=100)


class WorkTypeRequirementsRequest(BaseModel):
    qualification_ids: list[int] = Field(default_factory=list, max_length=100)
    equipment_type_ids: list[int] = Field(default_factory=list, max_length=100)


ERROR_RESPONSES = {
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@planning_router.post("/", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
@inject
async def create_project(body: CreateProjectRequest, repository: FromDishka[JobsRepository]) -> Any:
    try:
        return await repository.create_project({**body.model_dump(), "planning_one_day_enabled": True})
    except PlanningError as error:
        return _error_response(error)


@planning_router.get("/", response_model=list[ProjectResponse], responses=ERROR_RESPONSES)
@inject
async def list_projects(repository: FromDishka[JobsRepository]) -> Any:
    return await repository.list_projects()


@planning_router.post(
    "/{project_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
@inject
async def create_job(
    project_id: int,
    body: CreateJobRequest,
    repository: FromDishka[JobsRepository],
) -> Any:
    try:
        return await repository.create_job(project_id, body.model_dump())
    except PlanningError as error:
        return _error_response(error)


@planning_router.get(
    "/{project_id}/jobs",
    response_model=list[JobResponse],
    responses=ERROR_RESPONSES,
)
@inject
async def list_jobs(
    project_id: int,
    repository: FromDishka[JobsRepository],
) -> Any:
    try:
        return await repository.list_jobs(project_id)
    except PlanningError as error:
        return _error_response(error)


@planning_router.post(
    "/{project_id}/jobs/bulk",
    response_model=list[JobResponse],
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
@inject
async def create_jobs_bulk(
    project_id: int,
    body: BulkCreateJobsRequest,
    repository: FromDishka[JobsRepository],
) -> Any:
    try:
        return await repository.create_jobs(
            project_id, [item.model_dump() for item in body.jobs]
        )
    except PlanningError as error:
        return _error_response(error)


@planning_router.post("/{project_id}/engineers", response_model=EngineerResponse, status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
@inject
async def create_engineer(project_id: int, body: CreateEngineerRequest, repository: FromDishka[JobsRepository]) -> Any:
    try:
        return await repository.create_engineer(project_id, body.model_dump())
    except PlanningError as error:
        return _error_response(error)


@planning_router.get("/{project_id}/engineers", response_model=list[EngineerResponse], responses=ERROR_RESPONSES)
@inject
async def list_engineers(project_id: int, repository: FromDishka[JobsRepository]) -> Any:
    try:
        return await repository.list_engineers(project_id)
    except PlanningError as error:
        return _error_response(error)


@planning_router.post("/{project_id}/engineers/{engineer_id}/schedules", response_model=ScheduleResponse, status_code=status.HTTP_201_CREATED, responses=ERROR_RESPONSES)
@inject
async def create_schedule(project_id: int, engineer_id: int, body: CreateScheduleRequest, repository: FromDishka[JobsRepository]) -> Any:
    try:
        return await repository.create_schedule(engineer_id, body.model_dump())
    except PlanningError as error:
        return _error_response(error)


@planning_router.get("/{project_id}/engineers/{engineer_id}/schedules", response_model=list[ScheduleResponse], responses=ERROR_RESPONSES)
@inject
async def list_schedules(project_id: int, engineer_id: int, repository: FromDishka[JobsRepository]) -> Any:
    try:
        return await repository.list_schedules(engineer_id)
    except PlanningError as error:
        return _error_response(error)


@planning_router.put("/{project_id}/equipment-availability", response_model=EquipmentAvailabilityResponse, responses=ERROR_RESPONSES)
@inject
async def set_equipment_availability(project_id: int, body: EquipmentAvailabilityRequest, repository: FromDishka[JobsRepository]) -> Any:
    try:
        return await repository.set_equipment_availability(project_id, body.model_dump())
    except PlanningError as error:
        return _error_response(error)


@planning_router.get("/{project_id}/equipment-availability", response_model=list[EquipmentAvailabilityResponse], responses=ERROR_RESPONSES)
@inject
async def list_equipment_availability(project_id: int, repository: FromDishka[JobsRepository], availability_date: date | None = None) -> Any:
    try:
        return await repository.list_equipment_availability(project_id, availability_date)
    except PlanningError as error:
        return _error_response(error)


async def _catalog_create(project_id, body, repository, table):
    try:
        values = {key: value for key, value in body.model_dump(exclude_none=True).items() if key in table.c}
        return await repository.create_catalog_item(table, project_id, values)
    except PlanningError as error:
        return _error_response(error)


@planning_router.post("/{project_id}/qualifications", response_model=CatalogResponse, status_code=status.HTTP_201_CREATED)
@inject
async def create_qualification(project_id: int, body: CatalogRequest, repository: FromDishka[JobsRepository]) -> Any:
    return await _catalog_create(project_id, body, repository, qualifications)


@planning_router.get("/{project_id}/qualifications", response_model=list[CatalogResponse])
@inject
async def list_qualifications(project_id: int, repository: FromDishka[JobsRepository]) -> Any:
    return await repository.list_catalog_items(qualifications, project_id)


@planning_router.post("/{project_id}/equipment-types", response_model=CatalogResponse, status_code=status.HTTP_201_CREATED)
@inject
async def create_equipment_type(project_id: int, body: CatalogRequest, repository: FromDishka[JobsRepository]) -> Any:
    return await _catalog_create(project_id, body, repository, equipment_types)


@planning_router.get("/{project_id}/equipment-types", response_model=list[CatalogResponse])
@inject
async def list_equipment_types(project_id: int, repository: FromDishka[JobsRepository]) -> Any:
    return await repository.list_catalog_items(equipment_types, project_id)


@planning_router.post("/{project_id}/work-types", response_model=CatalogResponse, status_code=status.HTTP_201_CREATED)
@inject
async def create_work_type(project_id: int, body: CatalogRequest, repository: FromDishka[JobsRepository]) -> Any:
    return await _catalog_create(project_id, body, repository, work_types)


@planning_router.get("/{project_id}/work-types", response_model=list[CatalogResponse])
@inject
async def list_work_types(project_id: int, repository: FromDishka[JobsRepository]) -> Any:
    return await repository.list_catalog_items(work_types, project_id)


@planning_router.put("/{project_id}/engineers/{engineer_id}/qualifications", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def replace_engineer_qualifications(project_id: int, engineer_id: int, body: EngineerQualificationsRequest, repository: FromDishka[JobsRepository]) -> None:
    await repository.replace_engineer_qualifications(engineer_id, body.qualification_ids)


@planning_router.put("/{project_id}/work-types/{work_type_id}/requirements", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def replace_work_type_requirements(project_id: int, work_type_id: int, body: WorkTypeRequirementsRequest, repository: FromDishka[JobsRepository]) -> None:
    await repository.replace_work_type_requirements(work_type_id, body.qualification_ids, body.equipment_type_ids)


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
