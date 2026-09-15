from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter

from planning.application.errors import PlanningError
from planning.application.interactors.get_planning_run import GetPlanningRunInteractor
from planning.presentation.http.base.error_handler import planning_error_response
from planning.presentation.http.base.schemas import ERROR_RESPONSES
from planning.presentation.http.planning_runs.schemas import (
    PlanningRunDetailsResponse,
)

get_run_router = APIRouter()


@get_run_router.get(
    "/{project_id}/planning/runs/{run_id}",
    response_model=PlanningRunDetailsResponse,
    responses=ERROR_RESPONSES,
)
@inject
async def get_run(
    project_id: int,
    run_id: int,
    interactor: FromDishka[GetPlanningRunInteractor],
) -> Any:
    try:
        return await interactor(project_id, run_id)
    except PlanningError as error:
        return planning_error_response(error)
