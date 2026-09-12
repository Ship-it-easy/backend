from datetime import date
from typing import Any

from planning.application.access import ProjectAccess
from planning.application.errors import InvalidPlanningRequest
from planning.application.interfaces.project_management_repositories import (
    EngineerManagementRepository,
)


class _EngineerInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerManagementRepository,
    ):
        self._access = access
        self._repository = repository


class ListEngineersInteractor(_EngineerInteractor):
    async def __call__(self) -> list[dict[str, Any]]:
        _, project_id = await self._access.dispatcher()
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
    async def __call__(self, engineer_id: int) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_engineer(project_id, engineer_id)


class UpdateEngineerInteractor(_EngineerInteractor):
    async def __call__(
        self,
        engineer_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        if "start_address" in values and (
            "start_latitude" not in values or "start_longitude" not in values
        ):
            values.update(start_latitude=None, start_longitude=None)
        return await self._repository.update_engineer(
            project_id, engineer_id, values, qualification_ids
        )


class ReplaceEngineerScheduleInteractor(_EngineerInteractor):
    async def __call__(
        self,
        engineer_id: int,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        _, project_id = await self._access.dispatcher()
        dates: list[date] = [entry["work_date"] for entry in entries]
        if len(dates) != len(set(dates)):
            raise InvalidPlanningRequest(
                "Schedule dates must be unique", code="DUPLICATE_DATE"
            )
        return await self._repository.replace_schedule(project_id, engineer_id, entries)
