from typing import Any
from uuid import UUID

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, status

from planning.application.interactors.admin.users import (
    BlockUserInteractor,
    CreateDispatcherInteractor,
    CreateOwnerInteractor,
    CreateProjectUserInteractor,
    ListDispatchersInteractor,
    ListOwnersInteractor,
    ListProjectUsersInteractor,
    ReplaceDispatcherProjectsInteractor,
    ResetUserPasswordInteractor,
    UnblockUserInteractor,
)
from planning.presentation.http.admin.schemas import (
    DispatcherCreate,
    DispatcherProjectsPut,
    OwnerCreate,
    PasswordReset,
    UserCreate,
)

router = APIRouter()


@router.get("/dispatchers")
@inject
async def list_dispatchers(
    interactor: FromDishka[ListDispatchersInteractor],
) -> list[dict[str, Any]]:
    return await interactor()


@router.post("/dispatchers", status_code=status.HTTP_201_CREATED)
@inject
async def create_project_user(
    body: DispatcherCreate,
    interactor: FromDishka[CreateDispatcherInteractor],
) -> dict[str, Any]:
    return await interactor(body.login, body.password, body.project_ids)


@router.put("/dispatchers/{user_id}/projects")
@inject
async def replace_dispatcher_projects(
    user_id: UUID,
    body: DispatcherProjectsPut,
    interactor: FromDishka[ReplaceDispatcherProjectsInteractor],
) -> dict[str, Any]:
    return await interactor(user_id, body.project_ids)


@router.get("/owners")
@inject
async def list_owners(
    interactor: FromDishka[ListOwnersInteractor],
) -> list[dict[str, Any]]:
    return await interactor()


@router.post("/owners", status_code=status.HTTP_201_CREATED)
@inject
async def create_owner(
    body: OwnerCreate,
    interactor: FromDishka[CreateOwnerInteractor],
) -> dict[str, Any]:
    return await interactor(body.login, body.password)


@router.get("/projects/{project_id}/users")
@inject
async def list_project_users(
    project_id: int,
    interactor: FromDishka[ListProjectUsersInteractor],
) -> list[dict[str, Any]]:
    return await interactor(project_id)


@router.post("/projects/{project_id}/users", status_code=status.HTTP_201_CREATED)
@inject
async def create_dispatcher(
    project_id: int,
    body: UserCreate,
    interactor: FromDishka[CreateProjectUserInteractor],
) -> dict[str, Any]:
    return await interactor(
        project_id,
        body.login,
        body.password,
        body.role,
        body.engineer_id,
    )


@router.post("/users/{user_id}/reset-password")
@inject
async def reset_password(
    user_id: UUID,
    body: PasswordReset,
    interactor: FromDishka[ResetUserPasswordInteractor],
) -> dict[str, str]:
    return await interactor(user_id, body.password)


@router.post("/users/{user_id}/block")
@inject
async def block_user(
    user_id: UUID,
    interactor: FromDishka[BlockUserInteractor],
) -> dict[str, Any]:
    return await interactor(user_id)


@router.post("/users/{user_id}/unblock")
@inject
async def unblock_user(
    user_id: UUID,
    interactor: FromDishka[UnblockUserInteractor],
) -> dict[str, Any]:
    return await interactor(user_id)
