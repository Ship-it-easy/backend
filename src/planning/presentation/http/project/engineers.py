from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter

from planning.application.interactors.project.engineer_access import (
    BlockEngineerAccessInteractor,
    CreateEngineerAccessInteractor,
    ResetEngineerPasswordInteractor,
    UnblockEngineerAccessInteractor,
)
from planning.application.interactors.project.engineers import (
    CreateEngineerInteractor,
    GetEngineerInteractor,
    ListEngineersInteractor,
    ReplaceEngineerScheduleInteractor,
    UpdateEngineerInteractor,
)
from planning.presentation.http.project.schemas import (
    AccessCreate,
    AccessPasswordReset,
    EngineerCreate,
    EngineerPatch,
    SchedulePut,
)

router = APIRouter()


@router.get("/engineers")
@inject
async def engineer_list(
    interactor: FromDishka[ListEngineersInteractor],
) -> list[dict[str, Any]]:
    return await interactor()


@router.post("/engineers", status_code=201)
@inject
async def engineer_create(
    body: EngineerCreate,
    interactor: FromDishka[CreateEngineerInteractor],
) -> dict[str, Any]:
    values = body.model_dump(exclude={"qualification_ids"})
    return await interactor(values, body.qualification_ids)


@router.get("/engineers/{engineer_id}")
@inject
async def engineer_get(
    engineer_id: int,
    interactor: FromDishka[GetEngineerInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id)


@router.patch("/engineers/{engineer_id}")
@inject
async def engineer_patch(
    engineer_id: int,
    body: EngineerPatch,
    interactor: FromDishka[UpdateEngineerInteractor],
) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True, exclude={"qualification_ids"})
    return await interactor(engineer_id, values, body.qualification_ids)


@router.put("/engineers/{engineer_id}/schedule")
@inject
async def schedule_put(
    engineer_id: int,
    body: SchedulePut,
    interactor: FromDishka[ReplaceEngineerScheduleInteractor],
) -> dict[str, Any]:
    entries = [entry.model_dump() for entry in body.entries]
    return await interactor(engineer_id, entries)


@router.patch("/engineers/{engineer_id}/availability")
@inject
async def availability_patch(
    engineer_id: int,
    body: SchedulePut,
    interactor: FromDishka[ReplaceEngineerScheduleInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, [entry.model_dump() for entry in body.entries])


@router.post("/engineers/{engineer_id}/access", status_code=201)
@inject
async def engineer_access(
    engineer_id: int,
    body: AccessCreate,
    interactor: FromDishka[CreateEngineerAccessInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id, body.login, body.password)


@router.post("/engineers/{engineer_id}/reset-password")
@inject
async def engineer_password(
    engineer_id: int,
    body: AccessPasswordReset,
    interactor: FromDishka[ResetEngineerPasswordInteractor],
) -> dict[str, str]:
    return await interactor(engineer_id, body.password)


@router.post("/engineers/{engineer_id}/access/block")
@inject
async def engineer_access_block(
    engineer_id: int,
    interactor: FromDishka[BlockEngineerAccessInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id)


@router.post("/engineers/{engineer_id}/access/unblock")
@inject
async def engineer_access_unblock(
    engineer_id: int,
    interactor: FromDishka[UnblockEngineerAccessInteractor],
) -> dict[str, Any]:
    return await interactor(engineer_id)
