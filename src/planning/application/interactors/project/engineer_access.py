from typing import Any

from auth.application.interfaces.password_hasher import PasswordHasher
from auth.domain.entities.user import RawPassword
from planning.application.access import ProjectAccess
from planning.application.interfaces.project_management_repositories import (
    EngineerAccountRepository,
)


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
    async def __call__(
        self,
        engineer_id: int,
        login: str,
        password: str,
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.create_account(
            project_id,
            engineer_id,
            login.strip(),
            self._hash(password),
        )


class ResetEngineerPasswordInteractor(_EngineerAccessInteractor):
    async def __call__(self, engineer_id: int, password: str) -> dict[str, str]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.reset_password(
            project_id, engineer_id, self._hash(password)
        )


class BlockEngineerAccessInteractor(_EngineerAccessInteractor):
    async def __call__(self, engineer_id: int) -> dict[str, str]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.set_active(project_id, engineer_id, False)


class UnblockEngineerAccessInteractor(_EngineerAccessInteractor):
    async def __call__(self, engineer_id: int) -> dict[str, str]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.set_active(project_id, engineer_id, True)
