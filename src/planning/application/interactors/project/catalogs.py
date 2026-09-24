from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.project_management_repositories import (
    ProjectCatalogRepository,
)


class _CatalogInteractor:
    kind: str

    def __init__(self, access: ProjectAccess, repository: ProjectCatalogRepository):
        self._access = access
        self._repository = repository

    async def _project_id(
        self, scoped_project_id: int | None, *, write: bool = False
    ) -> int:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
            return project_id
        await self._access.project(scoped_project_id, write=write)
        return scoped_project_id


class ListQualificationsInteractor(_CatalogInteractor):
    async def __call__(
        self, scoped_project_id: int | None = None
    ) -> list[dict[str, Any]]:
        project_id = await self._project_id(scoped_project_id)
        return await self._repository.list_catalog(project_id, "qualification")


class CreateQualificationInteractor(_CatalogInteractor):
    async def __call__(
        self, values: dict[str, Any], scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.create_catalog(
            project_id, "qualification", values
        )


class UpdateQualificationInteractor(_CatalogInteractor):
    async def __call__(
        self,
        item_id: int,
        values: dict[str, Any],
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.update_catalog(
            project_id, "qualification", item_id, values
        )


class ListEquipmentTypesInteractor(_CatalogInteractor):
    async def __call__(
        self, scoped_project_id: int | None = None
    ) -> list[dict[str, Any]]:
        project_id = await self._project_id(scoped_project_id)
        return await self._repository.list_catalog(project_id, "equipment")


class CreateEquipmentTypeInteractor(_CatalogInteractor):
    async def __call__(
        self, values: dict[str, Any], scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.create_catalog(project_id, "equipment", values)


class UpdateEquipmentTypeInteractor(_CatalogInteractor):
    async def __call__(
        self,
        item_id: int,
        values: dict[str, Any],
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.update_catalog(
            project_id, "equipment", item_id, values
        )


class ClearEquipmentQuantityInteractor(_CatalogInteractor):
    async def __call__(
        self, item_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.clear_equipment(project_id, item_id)


class ListWorkTypesInteractor(_CatalogInteractor):
    async def __call__(
        self, scoped_project_id: int | None = None
    ) -> list[dict[str, Any]]:
        project_id = await self._project_id(scoped_project_id)
        return await self._repository.list_work_types(project_id)


class CreateWorkTypeInteractor(_CatalogInteractor):
    async def __call__(
        self,
        values: dict[str, Any],
        qualification_ids: list[int],
        equipment_type_ids: list[int],
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.create_work_type(
            project_id,
            values,
            qualification_ids,
            equipment_type_ids,
        )


class UpdateWorkTypeInteractor(_CatalogInteractor):
    async def __call__(
        self,
        item_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
        equipment_type_ids: list[int] | None,
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        project_id = await self._project_id(scoped_project_id, write=True)
        return await self._repository.update_work_type(
            project_id,
            item_id,
            values,
            qualification_ids,
            equipment_type_ids,
        )
