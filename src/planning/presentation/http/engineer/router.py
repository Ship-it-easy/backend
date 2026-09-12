from fastapi import APIRouter

from planning.presentation.http.engineer.assignments import router as assignments_router

engineer_router = APIRouter(prefix="/api/engineer", tags=["Engineer"])

engineer_router.include_router(assignments_router)
