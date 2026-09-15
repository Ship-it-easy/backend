from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, Query, status

from planning.application.interactors.admin.projects import (
    BlockProjectInteractor,
    CreateAdminProjectInteractor,
    GetAdminProjectInteractor,
    ListAdminProjectsInteractor,
    UnblockProjectInteractor,
    UpdateAdminProjectInteractor,
)
from planning.presentation.http.admin.schemas import ProjectCreate, ProjectPatch

router = APIRouter()


@router.get("/projects")
@inject
async def list_projects(
    interactor: FromDishka[ListAdminProjectsInteractor],
    search: str | None = None,
    project_status: str | None = Query(default=None, alias="status"),
) -> list[dict[str, Any]]:
    return await interactor(search, project_status)


@router.post("/projects", status_code=status.HTTP_201_CREATED)
@inject
async def create_project(
    body: ProjectCreate,
    interactor: FromDishka[CreateAdminProjectInteractor],
) -> dict[str, Any]:
    return await interactor(body.model_dump())


@router.get("/projects/{project_id}")
@inject
async def get_project(
    project_id: int,
    interactor: FromDishka[GetAdminProjectInteractor],
) -> dict[str, Any]:
    return await interactor(project_id)


@router.patch("/projects/{project_id}")
@inject
async def patch_project(
    project_id: int,
    body: ProjectPatch,
    interactor: FromDishka[UpdateAdminProjectInteractor],
) -> dict[str, Any]:
    return await interactor(project_id, body.model_dump(exclude_none=True))


@router.post("/projects/{project_id}/block")
@inject
async def block_project(
    project_id: int,
    interactor: FromDishka[BlockProjectInteractor],
) -> dict[str, Any]:
    return await interactor(project_id)


@router.post("/projects/{project_id}/unblock")
@inject
async def unblock_project(
    project_id: int,
    interactor: FromDishka[UnblockProjectInteractor],
) -> dict[str, Any]:
    return await interactor(project_id)
