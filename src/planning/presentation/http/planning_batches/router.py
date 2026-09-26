from datetime import date
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Header, Query, status

from planning.application.interactors.dynamic_planning import (
    GetCurrentProjectPlanInteractor,
    GetPlanningBaselineComparisonInteractor,
    GetPlanningBoardDayInteractor,
    GetPlanningBoardInteractor,
    GetPlanningEventInteractor,
    GetPlanningJobExplanationInteractor,
    GetProjectPlanVersionInteractor,
    ListProjectPlanVersionsInteractor,
    RetryPlanningBaselineInteractor,
    StartDynamicPlanningInteractor,
)
from planning.application.interactors.planning_batches import (
    GetPlanningBatchContextInteractor,
    GetPlanningBatchInteractor,
    ListPlanningBatchesInteractor,
    StartPlanningBatchInteractor,
    StopPlanningBatchInteractor,
    ValidateCurrentBatchDayInteractor,
)
from planning.application.interactors.project.planning import (
    GetPlanningConfigInteractor,
    PublishPlanningRunInteractor,
    UpdatePlanningConfigInteractor,
)
from planning.presentation.http.planning_batches.schemas import (
    StartPlanningBatchRequest,
    StartPlanningBatchResponse,
)
from planning.presentation.http.project.schemas import (
    PlanningConfigPatch,
    PublishRequest,
)

planning_batches_router = APIRouter()


@planning_batches_router.get("/{project_id}/planning-config")
@inject
async def get_project_planning_config(
    project_id: int,
    interactor: FromDishka[GetPlanningConfigInteractor],
) -> dict[str, Any]:
    return await interactor(project_id)


@planning_batches_router.patch("/{project_id}/planning-config")
@inject
async def update_project_planning_config(
    project_id: int,
    body: PlanningConfigPatch,
    interactor: FromDishka[UpdatePlanningConfigInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump(exclude_none=True), project_id)


@planning_batches_router.get("/{project_id}/planning/board")
@inject
async def get_planning_board(
    project_id: int,
    interactor: FromDishka[GetPlanningBoardInteractor],
    from_date: date | None = Query(default=None, alias="from"),
    days: int = Query(default=7, ge=7, le=7),
) -> dict[str, Any]:
    return await interactor(
        from_date=from_date,
        days=days,
        scoped_project_id=project_id,
    )


@planning_batches_router.get("/{project_id}/planning/board/{planning_date}")
@inject
async def get_planning_board_day(
    project_id: int,
    planning_date: date,
    interactor: FromDishka[GetPlanningBoardDayInteractor],
    plan_version_id: int | None = Query(default=None),
) -> dict[str, Any]:
    return await interactor(planning_date, plan_version_id, project_id)


@planning_batches_router.get(
    "/{project_id}/planning/current/days/{planning_date}/comparison"
)
@inject
async def get_planning_baseline_comparison(
    project_id: int,
    planning_date: date,
    interactor: FromDishka[GetPlanningBaselineComparisonInteractor],
    plan_version_id: int | None = Query(default=None),
) -> dict[str, Any]:
    return await interactor(planning_date, project_id, plan_version_id)


@planning_batches_router.post(
    "/{project_id}/planning/current/days/{planning_date}/comparison/engineers-expanded"
)
@inject
async def record_planning_comparison_engineers_expanded(
    project_id: int,
    planning_date: date,
    interactor: FromDishka[GetPlanningBaselineComparisonInteractor],
    planning_run_id: int = Query(...),
    plan_version_id: int | None = Query(default=None),
) -> dict[str, str]:
    return await interactor.record_engineers_expanded(
        planning_date, planning_run_id, project_id, plan_version_id
    )


@planning_batches_router.post(
    "/{project_id}/planning/runs/{planning_run_id}/baseline/retry",
    status_code=status.HTTP_202_ACCEPTED,
)
@inject
async def retry_planning_baseline(
    project_id: int,
    planning_run_id: int,
    interactor: FromDishka[RetryPlanningBaselineInteractor],
) -> dict[str, Any]:
    return await interactor(planning_run_id, project_id)


@planning_batches_router.get(
    "/{project_id}/planning/day-results/{day_result_id}/jobs/{job_id}/explanation"
)
@inject
async def get_planning_job_explanation(
    project_id: int,
    day_result_id: int,
    job_id: int,
    interactor: FromDishka[GetPlanningJobExplanationInteractor],
) -> dict[str, Any]:
    return await interactor(day_result_id, job_id, project_id)


@planning_batches_router.post(
    "/{project_id}/planning/events/manual", status_code=status.HTTP_202_ACCEPTED
)
@inject
async def start_dynamic_planning(
    project_id: int,
    interactor: FromDishka[StartDynamicPlanningInteractor],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
    ],
) -> dict[str, Any]:
    return await interactor(idempotency_key, project_id)


@planning_batches_router.get("/{project_id}/planning/events/{event_id}")
@inject
async def get_dynamic_planning_event(
    project_id: int,
    event_id: int,
    interactor: FromDishka[GetPlanningEventInteractor],
) -> dict[str, Any]:
    return await interactor(event_id, project_id)


@planning_batches_router.get("/{project_id}/planning/current")
@inject
async def get_current_dynamic_plan(
    project_id: int,
    interactor: FromDishka[GetCurrentProjectPlanInteractor],
) -> dict[str, Any]:
    return await interactor(project_id)


@planning_batches_router.get("/{project_id}/planning/versions")
@inject
async def list_dynamic_plan_versions(
    project_id: int,
    interactor: FromDishka[ListProjectPlanVersionsInteractor],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return await interactor(limit=limit, offset=offset, scoped_project_id=project_id)


@planning_batches_router.get("/{project_id}/planning/versions/{version_id}")
@inject
async def get_dynamic_plan_version(
    project_id: int,
    version_id: int,
    interactor: FromDishka[GetProjectPlanVersionInteractor],
) -> dict[str, Any]:
    return await interactor(version_id, project_id)


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
