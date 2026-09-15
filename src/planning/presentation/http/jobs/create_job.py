from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, status

from planning.application.errors import PlanningError
from planning.application.interactors.create_job import CreateJobInteractor
from planning.presentation.http.base.error_handler import planning_error_response
from planning.presentation.http.base.schemas import ERROR_RESPONSES
from planning.presentation.http.jobs.schemas import CreateJobRequest, JobResponse

create_job_router = APIRouter()


@create_job_router.post(
    "/{project_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
@inject
async def create_job(
    project_id: int,
    body: CreateJobRequest,
    interactor: FromDishka[CreateJobInteractor],
) -> Any:
    try:
        return await interactor(project_id, body.model_dump())
    except PlanningError as error:
        return planning_error_response(error)
