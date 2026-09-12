from datetime import date
from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

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
    return await interactor(run_id, body.confirm_unassigned)


@router.get("/daily-plans/{planning_date}")
@inject
async def daily_plan_get(
    planning_date: date,
    interactor: FromDishka[GetDailyPlanInteractor],
) -> dict[str, Any]:
    return await interactor(planning_date)
