from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interfaces.project_management_repositories import (
    AddressSearchProvider,
)


class SearchAddressesInteractor:
    def __init__(self, access: ProjectAccess, provider: AddressSearchProvider):
        self._access = access
        self._provider = provider

    async def __call__(self, query: str) -> list[dict[str, Any]]:
        await self._access.dispatcher()
        return await self._provider.search(query)
