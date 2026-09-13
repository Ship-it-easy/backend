import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.session import sessions_table
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.application.errors import (
    ConflictError,
    ObjectNotFoundError,
)
from planning.application.interfaces.admin_management_repositories import (
    AdminProjectRepository,
    AdminUserRepository,
)
from planning.application.management_dto import (
    AdminProjectUpdateState,
    ProjectUserValidationState,
    UserActivationState,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    engineers,
    planning_config,
    planning_runs,
    projects,
)


def _project(row: Any) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "id",
            "name",
            "planning_timezone",
            "status",
            "created_at",
            "updated_at",
        )
    }


def _user(row: Any) -> dict[str, Any]:
    role = row.role.value if hasattr(row.role, "value") else str(row.role).lower()
    return {
        "id": str(row.id),
        "login": row.username,
        "role": role,
        "project_id": row.project_id,
        "engineer_id": row.engineer_id,
        "status": "ACTIVE" if row.is_active else "BLOCKED",
    }


def _constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


class SqlaAdminProjectRepository(AdminProjectRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_projects(
        self, search: str | None, status: str | None
    ) -> list[dict[str, Any]]:
        query = select(projects)
        if search:
            query = query.where(projects.c.name.ilike(f"%{search}%"))
        if status:
            query = query.where(projects.c.status == status)
        rows = (
            (await self._session.execute(query.order_by(projects.c.name)))
            .mappings()
            .all()
        )
        return [_project(row) for row in rows]

    async def create_project(self, values: dict[str, Any]) -> dict[str, Any]:
        values.update(
            internal_code=f"PRJ-{uuid.uuid4().hex[:12].upper()}",
            planning_one_day_enabled=True,
            status="ACTIVE",
        )
        try:
            row = (
                (
                    await self._session.execute(
                        insert(projects).values(**values).returning(*projects.c)
                    )
                )
                .mappings()
                .one()
            )
            await self._session.execute(
                insert(planning_config).values(
                    project_id=row.id, version=1, active=True
                )
            )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "An active project with this name already exists",
                code="PROJECT_NAME_EXISTS",
            ) from error
        return _project(row)

    async def get_project(self, project_id: int) -> dict[str, Any]:
        row = (
            (
                await self._session.execute(
                    select(projects).where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ObjectNotFoundError("Project not found")
        return _project(row)

    async def load_update_state(self, project_id: int) -> AdminProjectUpdateState:
        exists = (
            await self._session.scalar(
                select(projects.c.id)
                .where(projects.c.id == project_id)
                .with_for_update()
            )
            is not None
        )
        has_runs = False
        if exists:
            has_runs = (
                await self._session.scalar(
                    select(planning_runs.c.id)
                    .where(planning_runs.c.project_id == project_id)
                    .limit(1)
                )
                is not None
            )
        return AdminProjectUpdateState(exists=exists, has_planning_runs=has_runs)

    async def save_project(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            row = (
                (
                    await self._session.execute(
                        update(projects)
                        .where(projects.c.id == project_id)
                        .values(**values, updated_at=datetime.now(timezone.utc))
                        .returning(*projects.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "An active project with this name already exists",
                code="PROJECT_NAME_EXISTS",
            ) from error
        if row is None:
            raise ObjectNotFoundError("Project not found")
        return _project(row)

    async def set_project_status(self, project_id: int, status: str) -> dict[str, Any]:
        try:
            row = (
                (
                    await self._session.execute(
                        update(projects)
                        .where(projects.c.id == project_id)
                        .values(status=status, updated_at=datetime.now(timezone.utc))
                        .returning(*projects.c)
                    )
                )
                .mappings()
                .one_or_none()
            )
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "Another active project has this name", code="PROJECT_NAME_EXISTS"
            ) from error
        if row is None:
            raise ObjectNotFoundError("Project not found")
        if status == "BLOCKED":
            user_ids = select(users_table.c.id).where(
                users_table.c.project_id == project_id
            )
            await self._session.execute(
                delete(sessions_table).where(sessions_table.c.user_id.in_(user_ids))
            )
        await self._session.commit()
        return _project(row)


class SqlaAdminUserRepository(AdminUserRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_owners(self) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    select(users_table)
                    .where(
                        users_table.c.role.in_([UserRoleEnum.OWNER, UserRoleEnum.ADMIN])
                    )
                    .order_by(users_table.c.username)
                )
            )
            .mappings()
            .all()
        )
        return [_user(row) for row in rows]

    async def list_project_users(self, project_id: int) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    select(users_table)
                    .where(users_table.c.project_id == project_id)
                    .order_by(users_table.c.username)
                )
            )
            .mappings()
            .all()
        )
        return [_user(row) for row in rows]

    async def create_user(
        self,
        login: str,
        password_hash: str,
        role: UserRoleEnum,
        project_id: int | None = None,
        engineer_id: int | None = None,
    ) -> dict[str, Any]:
        exists = await self._session.scalar(
            select(users_table.c.id).where(
                func.lower(users_table.c.username) == login.lower()
            )
        )
        if exists is not None:
            raise ConflictError("Login already exists", code="LOGIN_EXISTS")
        try:
            row = (
                (
                    await self._session.execute(
                        insert(users_table)
                        .values(
                            id=uuid.uuid4(),
                            username=login,
                            password_hash=password_hash,
                            is_active=True,
                            role=role,
                            is_verified=True,
                            project_id=project_id,
                            engineer_id=engineer_id,
                        )
                        .returning(*users_table.c)
                    )
                )
                .mappings()
                .one()
            )
        except IntegrityError as error:
            await self._session.rollback()
            if _constraint_name(error) == "uq_users_username_ci":
                raise ConflictError(
                    "Login already exists", code="LOGIN_EXISTS"
                ) from error
            raise
        return _user(row)

    async def get_project_user_validation(
        self,
        project_id: int,
        engineer_id: int | None,
    ) -> ProjectUserValidationState:
        project = (
            await self._session.execute(
                select(projects.c.id, projects.c.status).where(
                    projects.c.id == project_id
                )
            )
        ).one_or_none()
        if project is None:
            return ProjectUserValidationState(False, False)
        engineer_belongs: bool | None = None
        if engineer_id is not None:
            engineer_belongs = (
                await self._session.scalar(
                    select(engineers.c.id).where(
                        engineers.c.id == engineer_id,
                        engineers.c.project_id == project_id,
                    )
                )
                is not None
            )
        return ProjectUserValidationState(
            project_exists=True,
            project_active=project.status == "ACTIVE",
            engineer_belongs_to_project=engineer_belongs,
        )

    async def reset_password(self, user_id: UUID, password_hash: str) -> dict[str, str]:
        await self._target(user_id)
        await self._session.execute(
            update(users_table)
            .where(users_table.c.id == user_id)
            .values(password_hash=password_hash)
        )
        await self._session.execute(
            delete(sessions_table).where(sessions_table.c.user_id == user_id)
        )
        await self._session.commit()
        return {"status": "PASSWORD_RESET"}

    async def lock_activation(self, user_id: UUID) -> UserActivationState:
        active_owners = (
            (
                await self._session.execute(
                    select(users_table.c.id)
                    .where(
                        users_table.c.role.in_(
                            [UserRoleEnum.OWNER, UserRoleEnum.ADMIN]
                        ),
                        users_table.c.is_active.is_(True),
                    )
                    .order_by(users_table.c.id)
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        target = await self._target(user_id, for_update=True)
        return UserActivationState(
            id=target.id,
            login=target.username,
            role=target.role,
            project_id=target.project_id,
            engineer_id=target.engineer_id,
            active=target.is_active,
            active_owner_count=len(active_owners),
        )

    async def save_active(self, user_id: UUID, active: bool) -> dict[str, Any]:
        row = (
            (
                await self._session.execute(
                    update(users_table)
                    .where(users_table.c.id == user_id)
                    .values(is_active=active)
                    .returning(*users_table.c)
                )
            )
            .mappings()
            .one()
        )
        if not active:
            await self._session.execute(
                delete(sessions_table).where(sessions_table.c.user_id == user_id)
            )
        return _user(row)

    async def _target(self, user_id: UUID, *, for_update: bool = False) -> Any:
        query = select(users_table).where(users_table.c.id == user_id)
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).mappings().one_or_none()
        if row is None:
            raise ObjectNotFoundError("User not found")
        return row
