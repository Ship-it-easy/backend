from typing import Any, Protocol
from uuid import UUID

from auth.domain.user_role import UserRoleEnum
from planning.application.management_dto import (
    AdminProjectUpdateState,
    ProjectUserValidationState,
    UserActivationState,
)


class AdminProjectRepository(Protocol):
    async def list_projects(
        self, search: str | None, status: str | None
    ) -> list[dict[str, Any]]: ...
    async def create_project(self, values: dict[str, Any]) -> dict[str, Any]: ...
    async def get_project(self, project_id: int) -> dict[str, Any]: ...
    async def load_update_state(self, project_id: int) -> AdminProjectUpdateState: ...
    async def save_project(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def set_project_status(
        self, project_id: int, status: str
    ) -> dict[str, Any]: ...


class AdminUserRepository(Protocol):
    async def list_owners(self) -> list[dict[str, Any]]: ...
    async def list_project_users(self, project_id: int) -> list[dict[str, Any]]: ...
    async def create_user(
        self,
        login: str,
        password_hash: str,
        role: UserRoleEnum,
        project_id: int | None = None,
        engineer_id: int | None = None,
    ) -> dict[str, Any]: ...
    async def get_project_user_validation(
        self, project_id: int, engineer_id: int | None
    ) -> ProjectUserValidationState: ...
    async def reset_password(
        self, user_id: UUID, password_hash: str
    ) -> dict[str, str]: ...
    async def lock_activation(self, user_id: UUID) -> UserActivationState: ...
    async def save_active(
        self, user_id: UUID, active: bool, actor_user_id: UUID | None = None
    ) -> dict[str, Any]: ...
