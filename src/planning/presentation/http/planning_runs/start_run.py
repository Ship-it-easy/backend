from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from planning.application.errors import PlanningError
from planning.application.interactors.start_planning_run import (
    StartPlanningRunInteractor,
)
from planning.presentation.http.base.error_handler import planning_error_response
from planning.presentation.http.base.schemas import ERROR_RESPONSES
from planning.presentation.http.planning_runs.schemas import (
    StartPlanningRunRequest,
    StartPlanningRunResponse,
)

start_run_router = APIRouter()


@start_run_router.post(
    "/{project_id}/planning/runs",
    response_model=StartPlanningRunResponse,
    status_code=status.HTTP_201_CREATED,
    responses=ERROR_RESPONSES,
)
@inject
async def start_run(
    project_id: int,
    body: StartPlanningRunRequest,
    interactor: FromDishka[StartPlanningRunInteractor],
) -> dict[str, Any] | JSONResponse:
    try:
        return await interactor(project_id, body.planning_date, body.timezone)
    except PlanningError as error:
        return planning_error_response(error)
