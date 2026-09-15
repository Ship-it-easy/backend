from typing import Any

from planning.application.interfaces.jobs_repository import JobsRepository


class CreateJobInteractor:
    def __init__(self, repository: JobsRepository):
        self._repository = repository

    async def __call__(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._repository.create_job(project_id, values)
