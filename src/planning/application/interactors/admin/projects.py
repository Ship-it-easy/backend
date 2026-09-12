from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.admin_management_repositories import (
    AdminProjectRepository,
)


class _AdminProjectInteractor:
    def __init__(self, access: ProjectAccess, repository: AdminProjectRepository):
        self._access = access
        self._repository = repository


class ListAdminProjectsInteractor(_AdminProjectInteractor):
    async def __call__(
        self,
        search: str | None,
        status: str | None,
    ) -> list[dict[str, Any]]:
        await self._access.owner()
        return await self._repository.list_projects(search, status)


class CreateAdminProjectInteractor(_AdminProjectInteractor):
    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        await self._access.owner()
        values["name"] = values["name"].strip()
        return await self._repository.create_project(values)


class GetAdminProjectInteractor(_AdminProjectInteractor):
    async def __call__(self, project_id: int) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.get_project(project_id)


class UpdateAdminProjectInteractor(_AdminProjectInteractor):
    async def __call__(
        self,
        project_id: int,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.update_project(project_id, values)


class BlockProjectInteractor(_AdminProjectInteractor):
    async def __call__(self, project_id: int) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.set_project_status(project_id, "BLOCKED")


class UnblockProjectInteractor(_AdminProjectInteractor):
    async def __call__(self, project_id: int) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.set_project_status(project_id, "ACTIVE")
