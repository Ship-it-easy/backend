from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, File, UploadFile, status

from planning.application.errors import PlanningError
from planning.application.interactors.create_job import CreateJobInteractor
from planning.application.interactors.job_import import ProjectJobImportInteractor
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


@create_job_router.post("/{project_id}/jobs/import/preview")
@inject
async def import_preview(
    project_id: int,
    interactor: FromDishka[ProjectJobImportInteractor],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    return await interactor(
        await file.read(), apply=False, scoped_project_id=project_id
    )


@create_job_router.post("/{project_id}/jobs/import/apply", status_code=201)
@inject
async def import_apply(
    project_id: int,
    interactor: FromDishka[ProjectJobImportInteractor],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    return await interactor(await file.read(), apply=True, scoped_project_id=project_id)
