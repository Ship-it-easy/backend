from datetime import date
from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.project_management_repositories import (
    PlanningManagementRepository,
)


class _PlanningManagementInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: PlanningManagementRepository,
    ):
        self._access = access
        self._repository = repository


class GetPlanningConfigInteractor(_PlanningManagementInteractor):
    async def __call__(self) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_config(project_id)


class UpdatePlanningConfigInteractor(_PlanningManagementInteractor):
    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.update_config(project_id, values)


class CheckPlanningReadinessInteractor(_PlanningManagementInteractor):
    async def __call__(self, planning_date: date) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.readiness(project_id, planning_date)


class PublishPlanningRunInteractor(_PlanningManagementInteractor):
    async def __call__(
        self,
        run_id: int,
        confirm_unassigned: bool,
    ) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        return await self._repository.publish_run(
            project_id,
            run_id,
            user.id,
            confirm_unassigned,
        )


class GetDailyPlanInteractor(_PlanningManagementInteractor):
    async def __call__(self, planning_date: date) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_daily_plan(project_id, planning_date)
