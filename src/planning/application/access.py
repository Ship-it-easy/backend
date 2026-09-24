from dataclasses import dataclass

from auth.application.interfaces.identity_provider import IdentityProvider
from auth.domain.entities.user import User
from auth.domain.user_role import UserRoleEnum, is_dispatcher, is_owner
from planning.application.errors import (
    AccessDeniedError,
    ObjectNotFoundError,
    ProjectBlockedError,
)
from planning.application.interfaces.project_access_repository import (
    ProjectAccessRepository,
)


@dataclass(slots=True)
class ProjectAccess:
    identity_provider: IdentityProvider
    repository: ProjectAccessRepository

    async def user(self) -> User:
        return await self.identity_provider.get_current_user()

    async def owner(self) -> User:
        user = await self.user()
        if not is_owner(user.role):
            raise AccessDeniedError("Owner role is required")
        return user

    async def dispatcher(self) -> tuple[User, int]:
        """Resolve the legacy implicit dispatcher scope.

        New code must pass an explicit project to :meth:`project`.  Keeping
        this resolver allows old single-project API clients to transition
        safely, while refusing an ambiguous multi-project request.
        """
        user = await self.user()
        if not is_dispatcher(user.role):
            raise AccessDeniedError("Dispatcher role is required")
        available = await self.repository.list_dispatcher_projects(
            user.id, active_only=False
        )
        if len(available) != 1:
            raise AccessDeniedError("Explicit project scope is required")
        project_id = int(available[0]["id"])
        await self._active_project(project_id)
        return user, project_id

    async def project(self, project_id: int, *, write: bool = False) -> User:
        user = await self.user()
        if user.role is UserRoleEnum.OWNER:
            project_status = await self.repository.get_project_status(project_id)
            if project_status is None:
                raise ObjectNotFoundError("Project not found")
            if write and project_status != "ACTIVE":
                raise ProjectBlockedError("Project is blocked")
            return user
        has_access = is_dispatcher(
            user.role
        ) and await self.repository.dispatcher_has_project(user.id, project_id)
        if not has_access:
            # Do not reveal the existence of another tenant's data.
            raise ObjectNotFoundError("Object not found")
        # A blocked project is not part of an operational dispatcher session,
        # for reads or writes. Owners may still inspect blocked projects.
        await self._active_project(project_id)
        return user

    async def engineer(self) -> tuple[User, int, int]:
        user = await self.user()
        if (
            user.role is not UserRoleEnum.ENGINEER
            or user.project_id is None
            or user.engineer_id is None
        ):
            raise AccessDeniedError("Engineer role is required")
        await self._active_project(user.project_id)
        return user, int(user.project_id), int(user.engineer_id)

    async def _active_project(self, project_id: int) -> None:
        project_status = await self.repository.get_project_status(project_id)
        if project_status is None:
            raise ObjectNotFoundError("Project not found")
        if project_status != "ACTIVE":
            raise ProjectBlockedError("Project is blocked")
