from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
)


class SearchAddressesInteractor:
    def __init__(self, access: ProjectAccess, provider: AddressSearchProvider):
        self._access = access
        self._provider = provider

    async def __call__(
        self, query: str, scoped_project_id: int | None = None
    ) -> list[dict[str, Any]]:
        if scoped_project_id is None:
            await self._access.dispatcher()
        else:
            await self._access.project(scoped_project_id)
        return await self._provider.search(query)
