from typing import Any, Protocol
from uuid import UUID


class ProjectAccessRepository(Protocol):
    async def get_project_status(self, project_id: int) -> str | None: ...
    async def dispatcher_has_project(self, user_id: UUID, project_id: int) -> bool: ...
    async def list_dispatcher_projects(
        self, user_id: UUID, *, active_only: bool
    ) -> list[dict[str, Any]]: ...
