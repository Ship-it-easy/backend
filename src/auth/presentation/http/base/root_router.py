from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from auth.presentation.http.auth.auth_router import api_auth_router, auth_router
from planning.presentation.http.admin.router import admin_router
from planning.presentation.http.engineer.router import engineer_router
from planning.presentation.http.project.router import project_router
from planning.presentation.http.root_router import planning_router

root_router = APIRouter()


@root_router.get("/", tags=["General"])
async def redirect_to_docs() -> RedirectResponse:
    return RedirectResponse(url="docs/")


root_sub_routers = (
    auth_router,
    planning_router,
    admin_router,
    project_router,
    engineer_router,
)

for router in root_sub_routers:
    root_router.include_router(router)

# Keep the original /auth contract while exposing the specification auth API.
root_router.include_router(api_auth_router)
