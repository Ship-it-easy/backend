from typing import Any, Literal

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from planning.application.interactors.engineer.assignments import (
    CompleteAssignmentInteractor,
    GetAssignmentInteractor,
    ListAssignmentsInteractor,
    ReturnAssignmentInteractor,
    StartAssignmentInteractor,
)

router = APIRouter()


@router.get("/assignments")
@inject
async def assignment_list(
    interactor: FromDishka[ListAssignmentsInteractor],
    scope: Literal["today", "future", "history"] = Query("today"),
) -> list[dict[str, Any]]:
    return await interactor(scope)


@router.get("/assignments/{assignment_id}")
@inject
async def assignment_get(
    assignment_id: int,
    interactor: FromDishka[GetAssignmentInteractor],
) -> dict[str, Any]:
    return await interactor(assignment_id)


@router.post("/assignments/{assignment_id}/start")
@inject
async def assignment_start(
    assignment_id: int,
    interactor: FromDishka[StartAssignmentInteractor],
) -> dict[str, Any]:
    return await interactor(assignment_id)


@router.post("/assignments/{assignment_id}/complete")
@inject
async def assignment_complete(
    assignment_id: int,
    interactor: FromDishka[CompleteAssignmentInteractor],
) -> dict[str, Any]:
    return await interactor(assignment_id)


@router.post("/assignments/{assignment_id}/return-to-new")
@inject
async def assignment_return(
    assignment_id: int,
    interactor: FromDishka[ReturnAssignmentInteractor],
) -> dict[str, Any]:
    return await interactor(assignment_id)
