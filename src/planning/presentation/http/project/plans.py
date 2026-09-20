from datetime import date
from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Header, Query, status

from planning.application.interactors.dynamic_planning import (
    GetCurrentProjectPlanInteractor,
    GetPlanningBoardDayInteractor,
    GetPlanningBoardInteractor,
    GetPlanningEventInteractor,
    GetPlanningJobExplanationInteractor,
    GetProjectPlanVersionInteractor,
    ListProjectPlanVersionsInteractor,
    StartDynamicPlanningInteractor,
)
from planning.application.interactors.project.address_search import (
    SearchAddressesInteractor,
)
from planning.application.interactors.project.planning import (
    CheckPlanningReadinessInteractor,
    GetDailyPlanInteractor,
    GetPlanningConfigInteractor,
    PublishPlanningRunInteractor,
    UpdatePlanningConfigInteractor,
)
from planning.presentation.http.project.schemas import (
    PlanningConfigPatch,
    PublishRequest,
)

router = APIRouter()


@router.get("/planning/board")
@inject
async def planning_board(
    interactor: FromDishka[GetPlanningBoardInteractor],
    from_date: date | None = Query(default=None, alias="from"),
    days: int = Query(default=7, ge=7, le=7),
) -> dict[str, Any]:
    return await interactor(from_date=from_date, days=days)


@router.get("/planning/board/{planning_date}")
@inject
async def planning_board_day(
    planning_date: date,
    interactor: FromDishka[GetPlanningBoardDayInteractor],
    plan_version_id: int | None = Query(default=None),
) -> dict[str, Any]:
    return await interactor(planning_date, plan_version_id)


@router.get("/planning/day-results/{day_result_id}/jobs/{job_id}/explanation")
@inject
async def planning_job_explanation(
    day_result_id: int,
    job_id: int,
    interactor: FromDishka[GetPlanningJobExplanationInteractor],
) -> dict[str, Any]:
    return await interactor(day_result_id, job_id)


@router.post("/planning/events/manual", status_code=status.HTTP_202_ACCEPTED)
@inject
async def dynamic_planning_start(
    interactor: FromDishka[StartDynamicPlanningInteractor],
    idempotency_key: str = Header(
        alias="Idempotency-Key", min_length=1, max_length=255
    ),
) -> dict[str, Any]:
    return await interactor(idempotency_key)


@router.get("/planning/events/{event_id}")
@inject
async def dynamic_planning_event(
    event_id: int,
    interactor: FromDishka[GetPlanningEventInteractor],
) -> dict[str, Any]:
    return await interactor(event_id)


@router.get("/planning/current")
@inject
async def current_project_plan(
    interactor: FromDishka[GetCurrentProjectPlanInteractor],
) -> dict[str, Any]:
    return await interactor()


@router.get("/planning/versions")
@inject
async def project_plan_versions(
    interactor: FromDishka[ListProjectPlanVersionsInteractor],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    return await interactor(limit=limit, offset=offset)


@router.get("/planning/versions/{version_id}")
@inject
async def project_plan_version(
    version_id: int,
    interactor: FromDishka[GetProjectPlanVersionInteractor],
) -> dict[str, Any]:
    return await interactor(version_id)


@router.get("/address-suggestions")
@inject
async def address_suggestions(
    interactor: FromDishka[SearchAddressesInteractor],
    q: str = Query(min_length=3, max_length=300),
) -> list[dict[str, Any]]:
    return await interactor(q)


@router.get("/planning-config")
@inject
async def planning_config_get(
    interactor: FromDishka[GetPlanningConfigInteractor],
) -> dict[str, Any]:
    return await interactor()


@router.patch("/planning-config")
@inject
async def planning_config_patch(
    body: PlanningConfigPatch,
    interactor: FromDishka[UpdatePlanningConfigInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump(exclude_none=True))


@router.get("/planning/readiness")
@inject
async def planning_readiness(
    planning_date: date,
    interactor: FromDishka[CheckPlanningReadinessInteractor],
) -> dict[str, Any]:
    return await interactor(planning_date)


@router.post("/planning/runs/{run_id}/publish", status_code=201)
@inject
async def publish_run(
    run_id: int,
    body: PublishRequest,
    interactor: FromDishka[PublishPlanningRunInteractor],
) -> dict[str, Any]:
    return await interactor(
        run_id,
        body.confirm_unassigned,
        body.confirm_partial_batch,
    )


@router.get("/daily-plans/{planning_date}")
@inject
async def daily_plan_get(
    planning_date: date,
    interactor: FromDishka[GetDailyPlanInteractor],
) -> dict[str, Any]:
    return await interactor(planning_date)
