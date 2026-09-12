from datetime import date
from typing import Any

from auth.application.interfaces.identity_provider import IdentityProvider
from auth.domain.errors import AccessControlError
from auth.domain.user_role import UserRoleEnum, has_required_role
from planning.application.interfaces.planning_run_repository import (
    PlanningRunRepository,
)


class ListPlanningRunsInteractor:
    def __init__(
        self,
        identity_provider: IdentityProvider,
        repository: PlanningRunRepository,
    ):
        self._identity_provider = identity_provider
        self._repository = repository

    async def __call__(
        self,
        project_id: int,
        planning_date: date | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        await self._require_role(UserRoleEnum.USER)
        return await self._repository.list_runs(
            project_id, planning_date, status, min(limit, 100), offset
        )

    async def _require_role(self, required: UserRoleEnum) -> None:
        role = await self._identity_provider.get_role()
        if not has_required_role(role, required):
            raise AccessControlError("The required role does not exist.")
