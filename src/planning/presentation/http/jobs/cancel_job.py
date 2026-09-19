from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, status

from planning.application.interactors.project.jobs import CancelProjectJobInteractor

cancel_job_router = APIRouter()


@cancel_job_router.post(
    "/{project_id}/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED
)
@inject
async def cancel_job(
    project_id: int,
    job_id: int,
    interactor: FromDishka[CancelProjectJobInteractor],
) -> dict[str, Any]:
    return await interactor(job_id, project_id)
