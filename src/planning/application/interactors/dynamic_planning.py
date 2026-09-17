from typing import Any

from planning.application.access import ProjectAccess
from planning.application.errors import ObjectNotFoundError
from planning.application.interfaces.dynamic_planning_repository import (
    DynamicPlanningRepository,
)
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.application.interfaces.transaction_manager import TransactionManager


class StartDynamicPlanningInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: DynamicPlanningRepository,
        executor: PlanningBatchExecutor,
        transaction_manager: TransactionManager,
    ):
        self._access = access
        self._repository = repository
        self._executor = executor
        self._transaction_manager = transaction_manager

    async def __call__(
        self, idempotency_key: str, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            user, project_id = await self._access.dispatcher()
            status_url = "/api/project/planning/events/{event_id}"
        else:
            project_id = scoped_project_id
            user = await self._access.project(project_id, write=True)
            status_url = f"/api/projects/{project_id}/planning/events/{{event_id}}"
        event = await self._repository.enqueue(
            project_id, "MANUAL", user.id, idempotency_key
        )
        await self._transaction_manager.commit()
        self._executor.schedule_project(project_id)
        return {
            "planning_event_id": event["id"],
            "state": event["state"],
            "status_url": status_url.format(event_id=event["id"]),
        }


class GetPlanningEventInteractor:
    def __init__(
        self, access: ProjectAccess, repository: DynamicPlanningRepository
    ):
        self._access = access
        self._repository = repository

    async def __call__(
        self, event_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        event = await self._repository.get_event(project_id, event_id)
        if event is None:
            raise ObjectNotFoundError("Planning event not found")
        return event


class GetCurrentProjectPlanInteractor:
    def __init__(
        self, access: ProjectAccess, repository: DynamicPlanningRepository
    ):
        self._access = access
        self._repository = repository

    async def __call__(self, scoped_project_id: int | None = None) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        plan = await self._repository.get_current_plan(project_id)
        return plan or {"version": None, "assignments": [], "changes": []}


class ListProjectPlanVersionsInteractor:
    def __init__(
        self, access: ProjectAccess, repository: DynamicPlanningRepository
    ):
        self._access = access
        self._repository = repository

    async def __call__(
        self,
        *,
        limit: int,
        offset: int,
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        return await self._repository.list_plan_versions(project_id, limit, offset)


class GetProjectPlanVersionInteractor:
    def __init__(
        self, access: ProjectAccess, repository: DynamicPlanningRepository
    ):
        self._access = access
        self._repository = repository

    async def __call__(
        self, version_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        plan = await self._repository.get_plan_version(project_id, version_id)
        if plan is None:
            raise ObjectNotFoundError("Plan version not found")
        return plan
