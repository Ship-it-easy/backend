from datetime import date
from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, File, Query, UploadFile

from planning.application.interactors.job_import import ProjectJobImportInteractor
from planning.application.interactors.project.jobs import (
    CancelProjectJobInteractor,
    ChangeProjectJobStatusInteractor,
    CreateProjectJobInteractor,
    GetProjectJobInteractor,
    ListProjectJobsInteractor,
    UpdateProjectJobInteractor,
)
from planning.presentation.http.project.schemas import (
    JobCreate,
    JobPatch,
    JobStatusChange,
)

router = APIRouter()


@router.post("/jobs/{job_id}/cancel", status_code=202)
@inject
async def job_cancel(
    job_id: int,
    interactor: FromDishka[CancelProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(job_id)


@router.post("/jobs/import/preview")
@inject
async def job_import_preview(
    interactor: FromDishka[ProjectJobImportInteractor],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    return await interactor(await file.read(), apply=False)


@router.post("/jobs/import/apply", status_code=201)
@inject
async def job_import_apply(
    interactor: FromDishka[ProjectJobImportInteractor],
    file: UploadFile = File(...),
) -> dict[str, Any]:
    return await interactor(await file.read(), apply=True)


@router.get("/jobs")
@inject
async def job_list(
    interactor: FromDishka[ListProjectJobsInteractor],
    search: str | None = None,
    job_status: str | None = Query(default=None, alias="status"),
    sla_date: date | None = None,
    work_type_id: int | None = None,
    assigned: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return await interactor(
        search=search,
        status=job_status,
        sla_date=sla_date,
        work_type_id=work_type_id,
        assigned=assigned,
        limit=limit,
        offset=offset,
    )


@router.post("/jobs", status_code=201)
@inject
async def job_create(
    body: JobCreate,
    interactor: FromDishka[CreateProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump())


@router.get("/jobs/{job_id}")
@inject
async def job_get(
    job_id: int,
    interactor: FromDishka[GetProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(job_id)


@router.patch("/jobs/{job_id}")
@inject
async def job_patch(
    job_id: int,
    body: JobPatch,
    interactor: FromDishka[UpdateProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(job_id, body.model_dump(exclude_unset=True))


@router.post("/jobs/{job_id}/status")
@inject
async def job_status_change(
    job_id: int,
    body: JobStatusChange,
    interactor: FromDishka[ChangeProjectJobStatusInteractor],
    cancel_interactor: FromDishka[CancelProjectJobInteractor],
) -> dict[str, Any]:
    if body.status == "CANCELLED":
        return await cancel_interactor(job_id)
    return await interactor(job_id, body.status, body.reason)
