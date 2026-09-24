from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.planning_run_repository import (
    PlanningRunRepository,
)


class GetPlanningRunInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: PlanningRunRepository,
    ):
        self._access = access
        self._repository = repository

    async def __call__(self, project_id: int, run_id: int) -> dict[str, Any]:
        await self._require_project(project_id)
        return await self._repository.get_run(project_id, run_id)

    async def _require_project(self, project_id: int) -> None:
        await self._access.project(project_id)
        await self._repository.get_project_timezone(project_id)
