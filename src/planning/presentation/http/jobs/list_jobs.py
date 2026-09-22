from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from planning.application.errors import PlanningError
from planning.application.interactors.list_jobs import ListJobsInteractor
from planning.presentation.http.base.error_handler import planning_error_response
from planning.presentation.http.base.schemas import ERROR_RESPONSES
from planning.presentation.http.jobs.schemas import JobResponse

list_jobs_router = APIRouter()


@list_jobs_router.get(
    "/{project_id}/jobs",
    response_model=list[JobResponse],
    responses=ERROR_RESPONSES,
)
@inject
async def list_jobs(
    project_id: int,
    interactor: FromDishka[ListJobsInteractor],
    import_batch_id: int | None = Query(default=None, ge=1),
) -> Any:
    try:
        return await interactor(project_id, import_batch_id)
    except PlanningError as error:
        return planning_error_response(error)
