from typing import Any
from uuid import UUID

from auth.application.interfaces.password_hasher import PasswordHasher
from auth.domain.entities.user import RawPassword
from auth.domain.user_role import UserRoleEnum
from planning.application.access import ProjectAccess
from planning.application.errors import (
    ConflictError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
    ProjectBlockedError,
)
from planning.application.interfaces.admin_management_repositories import (
    AdminUserRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager


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


class ListDispatchersInteractor(_AdminUserInteractor):
    async def __call__(self) -> list[dict[str, Any]]:
        await self._access.owner()
        return await self._repository.list_dispatchers()


class CreateOwnerInteractor(_AdminUserInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(self, login: str, password: str) -> dict[str, Any]:
        await self._access.owner()
        result = await self._repository.create_user(
            login.strip(),
            self._hash(password),
            UserRoleEnum.OWNER,
        )
        await self._transaction_manager.commit()
        return result


async def _validate_projects(
    repository: AdminUserRepository, project_ids: list[int]
) -> list[int]:
    normalized = list(dict.fromkeys(project_ids))
    existing = await repository.get_existing_project_ids(normalized)
    if existing != set(normalized):
        raise ObjectNotFoundError("Project not found")
    return normalized


class CreateDispatcherInteractor(_AdminUserInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(
        self, login: str, password: str, project_ids: list[int]
    ) -> dict[str, Any]:
        owner = await self._access.owner()
        normalized = await _validate_projects(self._repository, project_ids)
        result = await self._repository.create_user(
            login.strip(),
            self._hash(password),
            UserRoleEnum.DISPATCHER,
        )
        result = await self._repository.replace_dispatcher_projects(
            UUID(result["id"]), normalized, owner.id
        )
        await self._transaction_manager.commit()
        return result


class ReplaceDispatcherProjectsInteractor(_AdminUserInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(
        self, user_id: UUID, project_ids: list[int]
    ) -> dict[str, Any]:
        owner = await self._access.owner()
        normalized = await _validate_projects(self._repository, project_ids)
        result = await self._repository.replace_dispatcher_projects(
            user_id, normalized, owner.id
        )
        await self._transaction_manager.commit()
        return result


class ListProjectUsersInteractor(_AdminUserInteractor):
    async def __call__(self, project_id: int) -> list[dict[str, Any]]:
        await self._access.owner()
        return await self._repository.list_project_users(project_id)


class CreateProjectUserInteractor(_AdminUserInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(
        self,
        project_id: int,
        login: str,
        password: str,
        role: UserRoleEnum | None,
        engineer_id: int | None,
    ) -> dict[str, Any]:
        owner = await self._access.owner()
        selected_role = role or UserRoleEnum.DISPATCHER
        if selected_role not in {
            UserRoleEnum.DISPATCHER,
            UserRoleEnum.ENGINEER,
        }:
            raise InvalidPlanningRequest(
                "Project user must be DISPATCHER or ENGINEER",
                code="INVALID_ROLE",
            )
        selected_engineer_id = (
            engineer_id if selected_role is UserRoleEnum.ENGINEER else None
        )
        if selected_role is UserRoleEnum.ENGINEER and selected_engineer_id is None:
            raise InvalidPlanningRequest(
                "engineer_id is required", code="ENGINEER_REQUIRED"
            )
        state = await self._repository.get_project_user_validation(
            project_id, selected_engineer_id
        )
        if not state.project_exists:
            raise ObjectNotFoundError("Project not found")
        if not state.project_active:
            raise ProjectBlockedError("Project is blocked")
        if state.engineer_belongs_to_project is False:
            raise InvalidPlanningRequest(
                "Engineer does not belong to project",
                code="CROSS_PROJECT_REFERENCE",
            )
        result = await self._repository.create_user(
            login.strip(),
            self._hash(password),
            selected_role,
            project_id if selected_role is UserRoleEnum.ENGINEER else None,
            selected_engineer_id,
        )
        if selected_role is UserRoleEnum.DISPATCHER:
            result = await self._repository.replace_dispatcher_projects(
                UUID(result["id"]), [project_id], owner.id
            )
        await self._transaction_manager.commit()
        return result


class ResetUserPasswordInteractor(_AdminUserInteractor):
    async def __call__(self, user_id: UUID, password: str) -> dict[str, str]:
        await self._access.owner()
        return await self._repository.reset_password(
            user_id,
            self._hash(password),
        )


class BlockUserInteractor(_AdminUserInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(self, user_id: UUID) -> dict[str, Any]:
        actor = await self._access.owner()
        state = await self._repository.lock_activation(user_id)
        if not state.active:
            await self._transaction_manager.commit()
            return state.response()
        if state.role is UserRoleEnum.OWNER and state.active_owner_count <= 1:
            raise ConflictError(
                "The last active owner cannot be blocked",
                code="LAST_ACTIVE_OWNER",
            )
        result = await self._repository.save_active(user_id, False, actor.id)
        await self._transaction_manager.commit()
        return result


class UnblockUserInteractor(_AdminUserInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: AdminUserRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(self, user_id: UUID) -> dict[str, Any]:
        actor = await self._access.owner()
        state = await self._repository.lock_activation(user_id)
        if state.active:
            await self._transaction_manager.commit()
            return state.response()
        result = await self._repository.save_active(user_id, True, actor.id)
        await self._transaction_manager.commit()
        return result
