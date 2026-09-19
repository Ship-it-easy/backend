from datetime import date
from typing import Any

from planning.application.access import ProjectAccess
from planning.application.errors import InvalidPlanningRequest
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.application.interfaces.project_management_repositories import (
    EngineerManagementRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager


class _EngineerInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerManagementRepository,
    ):
        self._access = access
        self._repository = repository


class ListEngineersInteractor(_EngineerInteractor):
    async def __call__(
        self, scoped_project_id: int | None = None
    ) -> list[dict[str, Any]]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        return await self._repository.list_engineers(project_id)


class CreateEngineerInteractor(_EngineerInteractor):
    async def __call__(
        self,
        values: dict[str, Any],
        qualification_ids: list[int],
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.create_engineer(
            project_id, values, qualification_ids
        )


class GetEngineerInteractor(_EngineerInteractor):
    async def __call__(
        self, engineer_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        return await self._repository.get_engineer(project_id, engineer_id)


class UpdateEngineerInteractor(_EngineerInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerManagementRepository,
        executor: PlanningBatchExecutor,
    ):
        super().__init__(access, repository)
        self._executor = executor

    async def __call__(
        self,
        engineer_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
    ) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        if "start_address" in values and (
            "start_latitude" not in values or "start_longitude" not in values
        ):
            values.update(start_latitude=None, start_longitude=None)
        result = await self._repository.update_engineer(
            project_id, engineer_id, values, qualification_ids, user.id
        )
        if result.get("planning_event_id"):
            self._executor.schedule_project(project_id)
        return result


class ReplaceEngineerScheduleInteractor(_EngineerInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerManagementRepository,
        executor: PlanningBatchExecutor,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository)
        self._executor = executor
        self._transaction_manager = transaction_manager

    async def __call__(
        self,
        engineer_id: int,
        entries: list[dict[str, Any]],
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            user, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            user = await self._access.project(project_id, write=True)
        dates: list[date] = [entry["work_date"] for entry in entries]
        if len(dates) != len(set(dates)):
            raise InvalidPlanningRequest(
                "Schedule dates must be unique", code="DUPLICATE_DATE"
            )
        result = await self._repository.replace_schedule_with_event(
            project_id, engineer_id, entries, user.id
        )
        await self._transaction_manager.commit()
        if result.get("planning_event_id"):
            self._executor.schedule_project(project_id)
        return result
