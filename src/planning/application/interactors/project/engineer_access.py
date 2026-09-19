from typing import Any

from auth.application.interfaces.password_hasher import PasswordHasher
from auth.domain.entities.user import RawPassword
from planning.application.access import ProjectAccess
from planning.application.errors import ObjectNotFoundError
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.application.interfaces.project_management_repositories import (
    EngineerAccountRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager


class _EngineerAccessInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerAccountRepository,
        password_hasher: PasswordHasher,
    ):
        self._access = access
        self._repository = repository
        self._password_hasher = password_hasher

    def _hash(self, password: str) -> str:
        return self._password_hasher.hash(RawPassword(password))


class CreateEngineerAccessInteractor(_EngineerAccessInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerAccountRepository,
        password_hasher: PasswordHasher,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository, password_hasher)
        self._transaction_manager = transaction_manager

    async def __call__(
        self,
        engineer_id: int,
        login: str,
        password: str,
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        if not await self._repository.engineer_belongs_to_project(
            project_id, engineer_id
        ):
            raise ObjectNotFoundError("Object not found")
        result = await self._repository.create_account(
            project_id,
            engineer_id,
            login.strip(),
            self._hash(password),
        )
        await self._transaction_manager.commit()
        return result


class ResetEngineerPasswordInteractor(_EngineerAccessInteractor):
    async def __call__(self, engineer_id: int, password: str) -> dict[str, str]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.reset_password(
            project_id, engineer_id, self._hash(password)
        )


class BlockEngineerAccessInteractor(_EngineerAccessInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerAccountRepository,
        password_hasher: PasswordHasher,
        executor: PlanningBatchExecutor,
    ):
        super().__init__(access, repository, password_hasher)
        self._executor = executor

    async def __call__(self, engineer_id: int) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        result = await self._repository.set_active(
            project_id, engineer_id, False, user.id
        )
        if result.get("planning_event_id"):
            self._executor.schedule_project(project_id)
        return result


class UnblockEngineerAccessInteractor(_EngineerAccessInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: EngineerAccountRepository,
        password_hasher: PasswordHasher,
        executor: PlanningBatchExecutor,
    ):
        super().__init__(access, repository, password_hasher)
        self._executor = executor

    async def __call__(self, engineer_id: int) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        result = await self._repository.set_active(
            project_id, engineer_id, True, user.id
        )
        if result.get("planning_event_id"):
            self._executor.schedule_project(project_id)
        return result
