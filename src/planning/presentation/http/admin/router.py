from fastapi import APIRouter

from planning.presentation.http.admin.projects import router as projects_router
from planning.presentation.http.admin.users import router as users_router

admin_router = APIRouter(prefix="/api/admin", tags=["Owner"])

admin_router.include_router(projects_router)
admin_router.include_router(users_router)
