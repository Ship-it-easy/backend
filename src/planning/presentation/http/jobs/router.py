from fastapi import APIRouter

from planning.presentation.http.jobs.create_job import create_job_router
from planning.presentation.http.jobs.list_jobs import list_jobs_router

jobs_router = APIRouter()
jobs_router.include_router(create_job_router)
jobs_router.include_router(list_jobs_router)
