from fastapi import APIRouter

from planning.presentation.http.planning_runs.get_run import get_run_router
from planning.presentation.http.planning_runs.list_runs import list_runs_router
from planning.presentation.http.planning_runs.start_run import start_run_router

planning_runs_router = APIRouter()
planning_runs_router.include_router(start_run_router)
planning_runs_router.include_router(get_run_router)
planning_runs_router.include_router(list_runs_router)
