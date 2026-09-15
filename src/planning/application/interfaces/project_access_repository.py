from typing import Protocol


class ProjectAccessRepository(Protocol):
    async def get_project_status(self, project_id: int) -> str | None: ...
