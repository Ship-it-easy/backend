from typing import Any

from planning.application.interfaces.jobs_repository import JobsRepository


class ListJobsInteractor:
    def __init__(self, repository: JobsRepository):
        self._repository = repository

    async def __call__(self, project_id: int) -> list[dict[str, Any]]:
        return await self._repository.list_jobs(project_id)
