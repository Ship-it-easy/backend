from typing import Any

from auth.application.interfaces.identity_provider import IdentityProvider
from auth.domain.errors import AccessControlError
from auth.domain.user_role import UserRoleEnum, is_dispatcher
from planning.application.errors import ProjectNotFound
from planning.application.interfaces.planning_run_repository import (
    PlanningRunRepository,
)


class GetPlanningRunInteractor:
    def __init__(
        self,
        identity_provider: IdentityProvider,
        repository: PlanningRunRepository,
    ):
        self._identity_provider = identity_provider
        self._repository = repository

    async def __call__(self, project_id: int, run_id: int) -> dict[str, Any]:
        await self._require_project(project_id)
        return await self._repository.get_run(project_id, run_id)

    async def _require_project(self, project_id: int) -> None:
        user = await self._identity_provider.get_current_user()
        if user.role is UserRoleEnum.OWNER:
            return
        if not is_dispatcher(user.role):
            raise AccessControlError("You do not have access to this project.")
        if user.project_id != project_id:
            raise ProjectNotFound("Project not found")
        await self._repository.get_project_timezone(project_id)
