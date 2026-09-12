import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.application.interfaces.password_hasher import PasswordHasher
from auth.domain.entities.user import RawPassword
from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.session import sessions_table
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.application.access import ProjectAccess
from planning.infrastructure.persistence_sqla.mappings.tables import (
    engineers,
    planning_config,
    planning_runs,
    projects,
)

admin_router = APIRouter(prefix="/api/admin", tags=["Owner"])


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    planning_timezone: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @field_validator("planning_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown IANA timezone") from error
        return value


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    planning_timezone: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip() if value is not None else None

    @field_validator("planning_timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as error:
                raise ValueError("unknown IANA timezone") from error
        return value


class OwnerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class UserCreate(OwnerCreate):
    role: UserRoleEnum | None = None
    engineer_id: int | None = None


class PasswordReset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=128)


def _project(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "planning_timezone": row.planning_timezone,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _user(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "login": row.username,
        "role": row.role.value if hasattr(row.role, "value") else str(row.role).lower(),
        "project_id": row.project_id,
        "engineer_id": row.engineer_id,
        "status": "ACTIVE" if row.is_active else "BLOCKED",
    }


@admin_router.get("/projects")
@inject
async def list_projects(
    access: FromDishka[ProjectAccess],
    session: FromDishka[AsyncSession],
    search: str | None = None,
    project_status: str | None = Query(default=None, alias="status"),
) -> list[dict[str, Any]]:
    await access.owner()
    query = select(projects)
    if search:
        query = query.where(projects.c.name.ilike(f"%{search}%"))
    if project_status:
        query = query.where(projects.c.status == project_status)
    rows = (await session.execute(query.order_by(projects.c.name))).mappings().all()
    return [_project(row) for row in rows]


@admin_router.post("/projects", status_code=status.HTTP_201_CREATED)
@inject
async def create_project(
    body: ProjectCreate,
    access: FromDishka[ProjectAccess],
    session: FromDishka[AsyncSession],
) -> dict[str, Any]:
    await access.owner()
    values = {
        "internal_code": f"PRJ-{uuid.uuid4().hex[:12].upper()}",
        "name": body.name.strip(),
        "planning_timezone": body.planning_timezone,
        "planning_one_day_enabled": True,
        "status": "ACTIVE",
    }
    try:
        row = (await session.execute(insert(projects).values(**values).returning(*projects.c))).mappings().one()
        await session.execute(insert(planning_config).values(project_id=row.id, version=1, active=True))
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "PROJECT_NAME_EXISTS", "message": "An active project with this name already exists"}) from error
    return _project(row)


@admin_router.get("/projects/{project_id}")
@inject
async def get_project(project_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    await access.owner()
    row = (await session.execute(select(projects).where(projects.c.id == project_id))).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return _project(row)


@admin_router.patch("/projects/{project_id}")
@inject
async def patch_project(project_id: int, body: ProjectPatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    await access.owner()
    values = body.model_dump(exclude_none=True)
    if "planning_timezone" in values:
        has_runs = await session.scalar(select(planning_runs.c.id).where(planning_runs.c.project_id == project_id).limit(1))
        if has_runs is not None:
            raise HTTPException(409, detail={"code": "TIMEZONE_LOCKED", "message": "Timezone cannot be changed after the first planning run"})
    values["updated_at"] = datetime.now(timezone.utc)
    try:
        row = (await session.execute(update(projects).where(projects.c.id == project_id).values(**values).returning(*projects.c))).mappings().one_or_none()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "PROJECT_NAME_EXISTS", "message": "An active project with this name already exists"}) from error
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    return _project(row)


async def _set_project_status(project_id: int, value: str, session: AsyncSession) -> dict[str, Any]:
    try:
        row = (await session.execute(update(projects).where(projects.c.id == project_id).values(status=value, updated_at=datetime.now(timezone.utc)).returning(*projects.c))).mappings().one_or_none()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "PROJECT_NAME_EXISTS", "message": "Another active project has this name"}) from error
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    if value == "BLOCKED":
        user_ids = select(users_table.c.id).where(users_table.c.project_id == project_id)
        await session.execute(delete(sessions_table).where(sessions_table.c.user_id.in_(user_ids)))
    await session.commit()
    return _project(row)


@admin_router.post("/projects/{project_id}/block")
@inject
async def block_project(project_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    await access.owner()
    return await _set_project_status(project_id, "BLOCKED", session)


@admin_router.post("/projects/{project_id}/unblock")
@inject
async def unblock_project(project_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    await access.owner()
    return await _set_project_status(project_id, "ACTIVE", session)


async def _create_user(body: OwnerCreate, role: UserRoleEnum, session: AsyncSession, hasher: PasswordHasher, project_id: int | None = None, engineer_id: int | None = None) -> dict[str, Any]:
    login = body.login.strip()
    exists = await session.scalar(select(users_table.c.id).where(func.lower(users_table.c.username) == login.lower()))
    if exists is not None:
        raise HTTPException(409, detail={"code": "LOGIN_EXISTS", "message": "Login already exists"})
    row = (await session.execute(insert(users_table).values(
        id=uuid.uuid4(), username=login, password_hash=hasher.hash(RawPassword(body.password)),
        is_active=True, role=role, is_verified=True, project_id=project_id, engineer_id=engineer_id,
    ).returning(*users_table.c))).mappings().one()
    await session.commit()
    return _user(row)


@admin_router.get("/owners")
@inject
async def list_owners(access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    await access.owner()
    rows = (await session.execute(select(users_table).where(users_table.c.role.in_([UserRoleEnum.OWNER, UserRoleEnum.ADMIN])).order_by(users_table.c.username))).mappings().all()
    return [_user(row) for row in rows]


@admin_router.post("/owners", status_code=status.HTTP_201_CREATED)
@inject
async def create_owner(body: OwnerCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession], hasher: FromDishka[PasswordHasher]) -> dict[str, Any]:
    await access.owner()
    return await _create_user(body, UserRoleEnum.OWNER, session, hasher)


@admin_router.get("/projects/{project_id}/users")
@inject
async def list_project_users(project_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    await access.owner()
    rows = (await session.execute(select(users_table).where(users_table.c.project_id == project_id).order_by(users_table.c.username))).mappings().all()
    return [_user(row) for row in rows]


@admin_router.post("/projects/{project_id}/users", status_code=status.HTTP_201_CREATED)
@inject
async def create_dispatcher(project_id: int, body: UserCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession], hasher: FromDishka[PasswordHasher]) -> dict[str, Any]:
    await access.owner()
    project = (await session.execute(select(projects.c.id, projects.c.status).where(projects.c.id == project_id))).one_or_none()
    if project is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Project not found"})
    if project.status != "ACTIVE":
        raise HTTPException(403, detail={"code": "PROJECT_BLOCKED", "message": "Project is blocked"})
    role = body.role or UserRoleEnum.DISPATCHER
    if role not in {UserRoleEnum.DISPATCHER, UserRoleEnum.ENGINEER}:
        raise HTTPException(422, detail={"code": "INVALID_ROLE", "message": "Project user must be DISPATCHER or ENGINEER"})
    engineer_id = body.engineer_id if role is UserRoleEnum.ENGINEER else None
    if role is UserRoleEnum.ENGINEER:
        if engineer_id is None:
            raise HTTPException(422, detail={"code": "ENGINEER_REQUIRED", "message": "engineer_id is required"})
        engineer = await session.scalar(select(engineers.c.id).where(engineers.c.id == engineer_id, engineers.c.project_id == project_id))
        if engineer is None:
            raise HTTPException(422, detail={"code": "CROSS_PROJECT_REFERENCE", "message": "Engineer does not belong to project"})
    return await _create_user(body, role, session, hasher, project_id=project_id, engineer_id=engineer_id)


async def _target_user(user_id: uuid.UUID, session: AsyncSession) -> Any:
    row = (await session.execute(select(users_table).where(users_table.c.id == user_id))).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "User not found"})
    return row


@admin_router.post("/users/{user_id}/reset-password")
@inject
async def reset_password(user_id: uuid.UUID, body: PasswordReset, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession], hasher: FromDishka[PasswordHasher]) -> dict[str, str]:
    await access.owner()
    await _target_user(user_id, session)
    await session.execute(update(users_table).where(users_table.c.id == user_id).values(password_hash=hasher.hash(RawPassword(body.password))))
    await session.execute(delete(sessions_table).where(sessions_table.c.user_id == user_id))
    await session.commit()
    return {"status": "PASSWORD_RESET"}


async def _set_user_active(user_id: uuid.UUID, active: bool, session: AsyncSession) -> dict[str, Any]:
    target = await _target_user(user_id, session)
    if not active and target.role in {UserRoleEnum.OWNER, UserRoleEnum.ADMIN}:
        count = await session.scalar(select(func.count()).select_from(users_table).where(users_table.c.role.in_([UserRoleEnum.OWNER, UserRoleEnum.ADMIN]), users_table.c.is_active.is_(True)))
        if int(count or 0) <= 1:
            raise HTTPException(409, detail={"code": "LAST_ACTIVE_OWNER", "message": "The last active owner cannot be blocked"})
    row = (await session.execute(update(users_table).where(users_table.c.id == user_id).values(is_active=active).returning(*users_table.c))).mappings().one()
    if not active:
        await session.execute(delete(sessions_table).where(sessions_table.c.user_id == user_id))
    await session.commit()
    return _user(row)


@admin_router.post("/users/{user_id}/block")
@inject
async def block_user(user_id: uuid.UUID, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    await access.owner()
    return await _set_user_active(user_id, False, session)


@admin_router.post("/users/{user_id}/unblock")
@inject
async def unblock_user(user_id: uuid.UUID, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    await access.owner()
    return await _set_user_active(user_id, True, session)
