from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.jobs_repository import JobsRepository


class ListJobsInteractor:
    def __init__(self, access: ProjectAccess, repository: JobsRepository):
        self._access = access
        self._repository = repository

    async def __call__(self, project_id: int) -> list[dict[str, Any]]:
        await self._access.project(project_id)
        return await self._repository.list_jobs(project_id)
