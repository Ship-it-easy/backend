from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter

from planning.application.interactors.project.catalogs import (
    ClearEquipmentQuantityInteractor,
    CreateEquipmentTypeInteractor,
    CreateQualificationInteractor,
    CreateWorkTypeInteractor,
    ListEquipmentTypesInteractor,
    ListQualificationsInteractor,
    ListWorkTypesInteractor,
    UpdateEquipmentTypeInteractor,
    UpdateQualificationInteractor,
    UpdateWorkTypeInteractor,
)
from planning.presentation.http.project.schemas import (
    EquipmentCreate,
    EquipmentPatch,
    NamedCreate,
    NamedPatch,
    WorkTypeCreate,
    WorkTypePatch,
)

router = APIRouter()


@router.get("/qualifications")
@inject
async def qualification_list(
    interactor: FromDishka[ListQualificationsInteractor],
) -> list[dict[str, Any]]:
    return await interactor()


@router.post("/qualifications", status_code=201)
@inject
async def qualification_create(
    body: NamedCreate,
    interactor: FromDishka[CreateQualificationInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump())


@router.patch("/qualifications/{item_id}")
@inject
async def qualification_patch(
    item_id: int,
    body: NamedPatch,
    interactor: FromDishka[UpdateQualificationInteractor],
) -> dict[str, Any]:
    return await interactor(
        item_id,
        body.model_dump(exclude_unset=True, exclude_none=True),
    )


@router.get("/equipment-types")
@inject
async def equipment_list(
    interactor: FromDishka[ListEquipmentTypesInteractor],
) -> list[dict[str, Any]]:
    return await interactor()


@router.post("/equipment-types", status_code=201)
@inject
async def equipment_create(
    body: EquipmentCreate,
    interactor: FromDishka[CreateEquipmentTypeInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump())


@router.patch("/equipment-types/{item_id}")
@inject
async def equipment_patch(
    item_id: int,
    body: EquipmentPatch,
    interactor: FromDishka[UpdateEquipmentTypeInteractor],
) -> dict[str, Any]:
    return await interactor(
        item_id,
        body.model_dump(exclude_unset=True, exclude_none=True),
    )


@router.post("/equipment-types/{item_id}/clear-quantity")
@inject
async def equipment_clear(
    item_id: int,
    interactor: FromDishka[ClearEquipmentQuantityInteractor],
) -> dict[str, Any]:
    return await interactor(item_id)


@router.get("/work-types")
@inject
async def work_type_list(
    interactor: FromDishka[ListWorkTypesInteractor],
) -> list[dict[str, Any]]:
    return await interactor()


@router.post("/work-types", status_code=201)
@inject
async def work_type_create(
    body: WorkTypeCreate,
    interactor: FromDishka[CreateWorkTypeInteractor],
) -> dict[str, Any]:
    values = body.model_dump(exclude={"qualification_ids", "equipment_type_ids"})
    return await interactor(
        values,
        body.qualification_ids,
        body.equipment_type_ids,
    )


@router.patch("/work-types/{item_id}")
@inject
async def work_type_patch(
    item_id: int,
    body: WorkTypePatch,
    interactor: FromDishka[UpdateWorkTypeInteractor],
) -> dict[str, Any]:
    values = body.update_values()
    return await interactor(
        item_id,
        values,
        body.qualification_ids,
        body.equipment_type_ids,
    )
