from fastapi import APIRouter

from planning.presentation.http.project.catalogs import router as catalogs_router
from planning.presentation.http.project.engineers import router as engineers_router
from planning.presentation.http.project.jobs import router as jobs_router
from planning.presentation.http.project.plans import router as plans_router

project_router = APIRouter(prefix="/api/project", tags=["Dispatcher"])

project_router.include_router(catalogs_router)
project_router.include_router(engineers_router)
project_router.include_router(jobs_router)
project_router.include_router(plans_router)
