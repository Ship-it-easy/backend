from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from planning.application.access import ProjectAccess
from planning.application.errors import InvalidPlanningRequest
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
    PlanningBatchRepository,
)


class StartPlanningBatchInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: PlanningBatchRepository,
        executor: PlanningBatchExecutor,
    ):
        self._access = access
        self._repository = repository
        self._executor = executor

    async def __call__(
        self, project_id: int, requested_start_date: date, idempotency_key: str
    ) -> dict[str, Any]:
        user = await self._access.project(project_id, write=True)
        timezone_name = await self._repository.get_project_timezone(project_id)
        try:
            local_today = (
                datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
            )
        except ZoneInfoNotFoundError as error:
            raise InvalidPlanningRequest(
                "Project has an invalid IANA timezone",
                code="PLANNING_CONFIGURATION_INVALID",
            ) from error
        if requested_start_date != local_today:
            raise InvalidPlanningRequest(
                "requested_start_date must equal the current project date",
                code="START_DATE_MUST_BE_TODAY",
            )
        batch, reused = await self._repository.create_or_reuse(
            project_id,
            requested_start_date,
            user.id,
            idempotency_key,
        )
        if not reused and batch["status"] == "CREATED":
            self._executor.schedule(int(batch["id"]))
        return {
            "planning_batch_id": batch["id"],
            "status": batch["status"],
            "status_url": f"/api/projects/{project_id}/planning/batches/{batch['id']}",
            "reuse": reused,
        }


class GetPlanningBatchContextInteractor:
    def __init__(self, access: ProjectAccess, repository: PlanningBatchRepository):
        self._access = access
        self._repository = repository

    async def __call__(self, project_id: int) -> dict[str, Any]:
        await self._access.project(project_id)
        timezone_name = await self._repository.get_project_timezone(project_id)
        try:
            planning_date = (
                datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
            )
        except ZoneInfoNotFoundError as error:
            raise InvalidPlanningRequest(
                "Project has an invalid IANA timezone",
                code="PLANNING_CONFIGURATION_INVALID",
            ) from error
        return {"planning_date": planning_date, "timezone": timezone_name}


class GetPlanningBatchInteractor:
    def __init__(self, access: ProjectAccess, repository: PlanningBatchRepository):
        self._access = access
        self._repository = repository

    async def __call__(self, project_id: int, batch_id: int) -> dict[str, Any]:
        await self._access.project(project_id)
        return await self._repository.get_batch(project_id, batch_id)


class ListPlanningBatchesInteractor:
    def __init__(self, access: ProjectAccess, repository: PlanningBatchRepository):
        self._access = access
        self._repository = repository

    async def __call__(
        self,
        project_id: int,
        status: str | None,
        date_from: date | None,
        date_to: date | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        await self._access.project(project_id)
        return await self._repository.list_batches(
            project_id, status, date_from, date_to, limit, offset
        )


class StopPlanningBatchInteractor:
    def __init__(self, access: ProjectAccess, repository: PlanningBatchRepository):
        self._access = access
        self._repository = repository

    async def __call__(self, project_id: int, batch_id: int) -> dict[str, Any]:
        await self._access.project(project_id, write=True)
        return await self._repository.request_stop(project_id, batch_id)


class ValidateCurrentBatchDayInteractor:
    def __init__(self, access: ProjectAccess, repository: PlanningBatchRepository):
        self._access = access
        self._repository = repository

    async def __call__(self, project_id: int, batch_id: int) -> dict[str, Any]:
        await self._access.project(project_id, write=True)
        return await self._repository.validate_current_day(project_id, batch_id)
