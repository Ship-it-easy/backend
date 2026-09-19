from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.jobs_repository import JobsRepository
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.application.interfaces.transaction_manager import TransactionManager


class CreateJobInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: JobsRepository,
        executor: PlanningBatchExecutor,
        transaction_manager: TransactionManager,
    ):
        self._access = access
        self._repository = repository
        self._executor = executor
        self._transaction_manager = transaction_manager

    async def __call__(self, project_id: int, values: dict[str, Any]) -> dict[str, Any]:
        user = await self._access.project(project_id, write=True)
        result = await self._repository.create_job(project_id, values, user.id)
        await self._transaction_manager.commit()
        self._executor.schedule_project(project_id)
        return result
