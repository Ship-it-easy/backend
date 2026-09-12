from datetime import date
from typing import Annotated, Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from planning.application.errors import PlanningError
from planning.application.interactors.list_planning_runs import (
    ListPlanningRunsInteractor,
)
from planning.presentation.http.base.error_handler import planning_error_response
from planning.presentation.http.base.schemas import ERROR_RESPONSES
from planning.presentation.http.planning_runs.schemas import PlanningRunRecord

list_runs_router = APIRouter()


@list_runs_router.get(
    "/{project_id}/planning/runs",
    response_model=list[PlanningRunRecord],
    responses=ERROR_RESPONSES,
)
@inject
async def list_runs(
    project_id: int,
    interactor: FromDishka[ListPlanningRunsInteractor],
    planning_date: date | None = None,
    run_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Any:
    try:
        return await interactor(
            project_id, planning_date, run_status, limit, offset
        )
    except PlanningError as error:
        return planning_error_response(error)
