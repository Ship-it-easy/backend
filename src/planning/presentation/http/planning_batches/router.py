from datetime import date
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Header, Query, status

from planning.application.interactors.planning_batches import (
    GetPlanningBatchContextInteractor,
    GetPlanningBatchInteractor,
    ListPlanningBatchesInteractor,
    StartPlanningBatchInteractor,
    StopPlanningBatchInteractor,
    ValidateCurrentBatchDayInteractor,
)
from planning.application.interactors.project.planning import (
    PublishPlanningRunInteractor,
)
from planning.presentation.http.planning_batches.schemas import (
    StartPlanningBatchRequest,
    StartPlanningBatchResponse,
)
from planning.presentation.http.project.schemas import PublishRequest

planning_batches_router = APIRouter()


@planning_batches_router.get("/{project_id}/planning/context")
@inject
async def planning_context(
    project_id: int,
    interactor: FromDishka[GetPlanningBatchContextInteractor],
) -> dict[str, Any]:
    return await interactor(project_id)


@planning_batches_router.post(
    "/{project_id}/planning/batches",
    response_model=StartPlanningBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@inject
async def start_batch(
    project_id: int,
    body: StartPlanningBatchRequest,
    interactor: FromDishka[StartPlanningBatchInteractor],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ],
) -> dict[str, Any]:
    return await interactor(project_id, body.requested_start_date, idempotency_key)


@planning_batches_router.get("/{project_id}/planning/batches/{batch_id}")
@inject
async def get_batch(
    project_id: int,
    batch_id: int,
    interactor: FromDishka[GetPlanningBatchInteractor],
) -> dict[str, Any]:
    return await interactor(project_id, batch_id)


@planning_batches_router.get("/{project_id}/planning/batches")
@inject
async def list_batches(
    project_id: int,
    interactor: FromDishka[ListPlanningBatchesInteractor],
    batch_status: str | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[dict[str, Any]]:
    return await interactor(project_id, batch_status, date_from, date_to, limit, offset)


@planning_batches_router.post("/{project_id}/planning/batches/{batch_id}/stop")
@inject
async def stop_batch(
    project_id: int,
    batch_id: int,
    interactor: FromDishka[StopPlanningBatchInteractor],
) -> dict[str, Any]:
    return await interactor(project_id, batch_id)


@planning_batches_router.post(
    "/{project_id}/planning/batches/{batch_id}/validate-current-day"
)
@inject
async def validate_current_day(
    project_id: int,
    batch_id: int,
    interactor: FromDishka[ValidateCurrentBatchDayInteractor],
) -> dict[str, Any]:
    return await interactor(project_id, batch_id)


@planning_batches_router.post(
    "/{project_id}/planning/runs/{run_id}/publish", status_code=201
)
@inject
async def publish_scoped_run(
    project_id: int,
    run_id: int,
    body: PublishRequest,
    interactor: FromDishka[PublishPlanningRunInteractor],
) -> dict[str, Any]:
    return await interactor(
        run_id,
        body.confirm_unassigned,
        body.confirm_partial_batch,
        project_id,
    )
