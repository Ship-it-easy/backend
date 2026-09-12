from typing import Any
from uuid import UUID

from auth.application.interfaces.password_hasher import PasswordHasher
from auth.domain.entities.user import RawPassword
from auth.domain.user_role import UserRoleEnum
from planning.application.access import ProjectAccess
from planning.application.interfaces.admin_management_repositories import (
    AdminUserRepository,
)


class _AdminUserInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
    ):
        self._access = access
        self._repository = repository
        self._password_hasher = password_hasher

    def _hash(self, password: str) -> str:
        return self._password_hasher.hash(RawPassword(password))


class ListOwnersInteractor(_AdminUserInteractor):
    async def __call__(self) -> list[dict[str, Any]]:
        await self._access.owner()
        return await self._repository.list_owners()


class CreateOwnerInteractor(_AdminUserInteractor):
    async def __call__(self, login: str, password: str) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.create_user(
            login.strip(),
            self._hash(password),
            UserRoleEnum.OWNER,
        )


class ListProjectUsersInteractor(_AdminUserInteractor):
    async def __call__(self, project_id: int) -> list[dict[str, Any]]:
        await self._access.owner()
        return await self._repository.list_project_users(project_id)


class CreateProjectUserInteractor(_AdminUserInteractor):
    async def __call__(
        self,
        project_id: int,
        login: str,
        password: str,
        role: UserRoleEnum | None,
        engineer_id: int | None,
    ) -> dict[str, Any]:
        await self._access.owner()
        selected_role = role or UserRoleEnum.DISPATCHER
        selected_engineer_id = (
            engineer_id if selected_role is UserRoleEnum.ENGINEER else None
        )
        await self._repository.validate_project_user(
            project_id,
            selected_role,
            selected_engineer_id,
        )
        return await self._repository.create_user(
            login.strip(),
            self._hash(password),
            selected_role,
            project_id,
            selected_engineer_id,
        )


class ResetUserPasswordInteractor(_AdminUserInteractor):
    async def __call__(self, user_id: UUID, password: str) -> dict[str, str]:
        await self._access.owner()
        return await self._repository.reset_password(
            user_id,
            self._hash(password),
        )


class BlockUserInteractor(_AdminUserInteractor):
    async def __call__(self, user_id: UUID) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.set_active(user_id, False)


class UnblockUserInteractor(_AdminUserInteractor):
    async def __call__(self, user_id: UUID) -> dict[str, Any]:
        await self._access.owner()
        return await self._repository.set_active(user_id, True)
