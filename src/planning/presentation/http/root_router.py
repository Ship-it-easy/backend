from fastapi import APIRouter

from planning.presentation.http.jobs.router import jobs_router
from planning.presentation.http.planning_batches.router import planning_batches_router
from planning.presentation.http.planning_runs.router import planning_runs_router

planning_router = APIRouter(prefix="/api/projects", tags=["Planning"])
planning_router.include_router(jobs_router)
planning_router.include_router(planning_runs_router)
planning_router.include_router(planning_batches_router)
