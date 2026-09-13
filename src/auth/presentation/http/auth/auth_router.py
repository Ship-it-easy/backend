from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from auth.application.interactors.log_in import LogInInteractor, LogInRequest
from auth.application.interactors.log_out import LogOutInteractor
from auth.application.interfaces.identity_provider import IdentityProvider
from auth.presentation.http.auth.log_in import log_in_router
from auth.presentation.http.auth.log_out import log_out_router

auth_router = APIRouter(
    prefix="/auth",
    tags=["Auth"],
)
me_router = APIRouter()
api_auth_router = APIRouter(prefix="/api/auth", tags=["Auth"])


class ApiLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1)
    password: str = Field(min_length=1)


@api_auth_router.post("/login", status_code=status.HTTP_200_OK)
@inject
async def api_login(
    body: ApiLoginRequest, interactor: FromDishka[LogInInteractor]
) -> None:
    return await interactor(
        LogInRequest(username=body.login, raw_password=body.password)
    )


@api_auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def api_logout(interactor: FromDishka[LogOutInteractor]) -> None:
    return await interactor()


auth_sub_routers = (log_in_router, log_out_router)

for router in auth_sub_routers:
    auth_router.include_router(router)


@me_router.get("/me")
@api_auth_router.get("/me")
@inject
async def me(identity_provider: FromDishka[IdentityProvider]) -> dict:
    user = await identity_provider.get_current_user()
    return {
        "id": str(user.id),
        "login": str(user.username),
        "role": user.role.value,
        "project_id": user.project_id,
        "engineer_id": user.engineer_id,
        "status": "ACTIVE" if user.is_active else "BLOCKED",
    }


auth_router.include_router(me_router)
