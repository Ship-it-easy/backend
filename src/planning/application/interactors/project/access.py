from typing import Any

from auth.domain.user_role import is_dispatcher
from planning.application.access import ProjectAccess
from planning.application.errors import AccessDeniedError


class ListDispatcherProjectsInteractor:
    def __init__(self, access: ProjectAccess):
        self._access = access

    async def __call__(self) -> list[dict[str, Any]]:
        user = await self._access.user()
        if not is_dispatcher(user.role):
            raise AccessDeniedError("Dispatcher role is required")
        return await self._access.repository.list_dispatcher_projects(
            user.id, active_only=True
        )
