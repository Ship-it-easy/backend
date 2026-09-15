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


class ListQualificationsInteractor(_CatalogInteractor):
    async def __call__(self) -> list[dict[str, Any]]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.list_catalog(project_id, "qualification")


class CreateQualificationInteractor(_CatalogInteractor):
    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.create_catalog(
            project_id, "qualification", values
        )


class UpdateQualificationInteractor(_CatalogInteractor):
    async def __call__(self, item_id: int, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.update_catalog(
            project_id, "qualification", item_id, values
        )


class ListEquipmentTypesInteractor(_CatalogInteractor):
    async def __call__(self) -> list[dict[str, Any]]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.list_catalog(project_id, "equipment")


class CreateEquipmentTypeInteractor(_CatalogInteractor):
    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.create_catalog(project_id, "equipment", values)


class UpdateEquipmentTypeInteractor(_CatalogInteractor):
    async def __call__(self, item_id: int, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.update_catalog(
            project_id, "equipment", item_id, values
        )


class ClearEquipmentQuantityInteractor(_CatalogInteractor):
    async def __call__(self, item_id: int) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.clear_equipment(project_id, item_id)


class ListWorkTypesInteractor(_CatalogInteractor):
    async def __call__(self) -> list[dict[str, Any]]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.list_work_types(project_id)


class CreateWorkTypeInteractor(_CatalogInteractor):
    async def __call__(
        self,
        values: dict[str, Any],
        qualification_ids: list[int],
        equipment_type_ids: list[int],
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
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
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.update_work_type(
            project_id,
            item_id,
            values,
            qualification_ids,
            equipment_type_ids,
        )
