from typing import Any

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter

from planning.application.interactors.project.access import (
    ListDispatcherProjectsInteractor,
)
from planning.presentation.http.project.catalogs import router as catalogs_router
from planning.presentation.http.project.engineers import router as engineers_router
from planning.presentation.http.project.jobs import router as jobs_router
from planning.presentation.http.project.plans import router as plans_router
from planning.presentation.http.project.traffic import router as traffic_router

project_router = APIRouter(prefix="/api/project", tags=["Dispatcher"])


@project_router.get("/available-projects")
@inject
async def available_projects(
    interactor: FromDishka[ListDispatcherProjectsInteractor],
) -> list[dict[str, Any]]:
    return await interactor()

project_router.include_router(catalogs_router)
project_router.include_router(engineers_router)
project_router.include_router(jobs_router)
project_router.include_router(plans_router)
project_router.include_router(traffic_router)
