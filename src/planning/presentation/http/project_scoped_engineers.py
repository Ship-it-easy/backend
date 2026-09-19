from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query

from planning.application.interactors.project.address_search import (
    SearchAddressesInteractor,
)
from planning.application.interactors.project.catalogs import ListWorkTypesInteractor
from planning.application.interactors.project.engineers import (
    GetEngineerInteractor,
    ListEngineersInteractor,
    ReplaceEngineerScheduleInteractor,
)
from planning.presentation.http.project.schemas import SchedulePut

router = APIRouter()


@router.get("/{project_id}/address-suggestions")
@inject
async def address_suggestions(
    project_id: int,
    interactor: FromDishka[SearchAddressesInteractor],
    q: str = Query(min_length=3, max_length=300),
) -> list[dict[str, Any]]:
    return await interactor(q, project_id)


@router.get("/{project_id}/work-types")
@inject
async def work_type_list(
    project_id: int,
    interactor: FromDishka[ListWorkTypesInteractor],
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.get("/{project_id}/engineers")
@inject
async def engineer_list(
    project_id: int,
    interactor: FromDishka[ListEngineersInteractor],
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.get("/{project_id}/engineers/{engineer_id}")
@inject
async def engineer_get(
    project_id: int,
    engineer_id: int,
    interactor: FromDishka[GetEngineerInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, project_id)


@router.patch("/{project_id}/engineers/{engineer_id}/availability")
@inject
async def availability_patch(
    project_id: int,
    engineer_id: int,
    body: SchedulePut,
    interactor: FromDishka[ReplaceEngineerScheduleInteractor],
) -> dict[str, Any]:
    entries = [entry.model_dump() for entry in body.entries]
    return await interactor(engineer_id, entries, project_id)
