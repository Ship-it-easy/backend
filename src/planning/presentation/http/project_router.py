import uuid
from datetime import date, datetime, time, timezone
from typing import Any, Literal

import httpx
from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.application.interfaces.password_hasher import PasswordHasher
from auth.domain.entities.user import RawPassword
from auth.domain.user_role import UserRoleEnum
from auth.entrypoint.config import PlanningServiceConfig
from auth.infrastructure.persistence_sqla.mappings.session import sessions_table
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.application.access import ProjectAccess
from planning.application.job_status import change_job_status
from planning.infrastructure.persistence.tables import (
    assignments,
    daily_plans,
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    job_status_history,
    jobs,
    plan_versions,
    planning_config,
    planning_route_jobs,
    planning_routes,
    planning_runs,
    qualifications,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)

project_router = APIRouter(prefix="/api/project", tags=["Dispatcher"])


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _dict(row: Any, *, hidden: tuple[str, ...] = ("internal_code", "code")) -> dict[str, Any]:
    return {key: value for key, value in dict(row).items() if key not in hidden}


async def _belongs(session: AsyncSession, table: Any, item_id: int, project_id: int) -> Any:
    row = (await session.execute(select(table).where(table.c.id == item_id, table.c.project_id == project_id))).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Object not found"})
    return row


class NamedCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    active: bool = True


class NamedPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool | None = None


class EquipmentCreate(NamedCreate):
    available_units: int = Field(default=0, ge=0)


class EquipmentPatch(NamedPatch):
    available_units: int | None = Field(default=None, ge=0)


class WorkTypeCreate(NamedCreate):
    default_service_duration_min: int = Field(gt=0)
    required_transport: Literal["CAR"] | None = None
    qualification_ids: list[int] = Field(default_factory=list)
    equipment_type_ids: list[int] = Field(default_factory=list)


class WorkTypePatch(NamedPatch):
    default_service_duration_min: int | None = Field(default=None, gt=0)
    required_transport: Literal["CAR"] | None = None
    qualification_ids: list[int] | None = None
    equipment_type_ids: list[int] | None = None


async def _catalog_list(table: Any, project_id: int, session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(table).where(table.c.project_id == project_id).order_by(table.c.name))).mappings().all()
    return [_dict(row) for row in rows]


async def _catalog_create(table: Any, prefix: str, body: BaseModel, project_id: int, session: AsyncSession) -> dict[str, Any]:
    values = body.model_dump(exclude={"qualification_ids", "equipment_type_ids"})
    values["code"] = _code(prefix)
    try:
        row = (await session.execute(insert(table).values(project_id=project_id, **values).returning(*table.c))).mappings().one()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "NAME_EXISTS", "message": "Name already exists in project"}) from error
    return _dict(row)


async def _catalog_patch(table: Any, item_id: int, body: BaseModel, project_id: int, session: AsyncSession) -> dict[str, Any]:
    await _belongs(session, table, item_id, project_id)
    values = {
        key: value
        for key, value in body.model_dump(
            exclude_unset=True,
            exclude={"qualification_ids", "equipment_type_ids"},
        ).items()
        if value is not None or key == "required_transport"
    }
    try:
        row = (await session.execute(update(table).where(table.c.id == item_id).values(**values).returning(*table.c))).mappings().one()
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "NAME_EXISTS", "message": "Name already exists in project"}) from error
    return _dict(row)


@project_router.get("/qualifications")
@inject
async def qualification_list(access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    _, project_id = await access.dispatcher()
    return await _catalog_list(qualifications, project_id, session)


@project_router.post("/qualifications", status_code=201)
@inject
async def qualification_create(body: NamedCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    return await _catalog_create(qualifications, "QUAL", body, project_id, session)


@project_router.patch("/qualifications/{item_id}")
@inject
async def qualification_patch(item_id: int, body: NamedPatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    return await _catalog_patch(qualifications, item_id, body, project_id, session)


@project_router.get("/equipment-types")
@inject
async def equipment_list(access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    _, project_id = await access.dispatcher()
    return await _catalog_list(equipment_types, project_id, session)


@project_router.post("/equipment-types", status_code=201)
@inject
async def equipment_create(body: EquipmentCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    return await _catalog_create(equipment_types, "EQUIP", body, project_id, session)


@project_router.patch("/equipment-types/{item_id}")
@inject
async def equipment_patch(item_id: int, body: EquipmentPatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    return await _catalog_patch(equipment_types, item_id, body, project_id, session)


@project_router.post("/equipment-types/{item_id}/clear-quantity")
@inject
async def equipment_clear(item_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    await _belongs(session, equipment_types, item_id, project_id)
    row = (await session.execute(update(equipment_types).where(equipment_types.c.id == item_id).values(available_units=0).returning(*equipment_types.c))).mappings().one()
    await session.commit()
    return _dict(row)


async def _validate_ids(session: AsyncSession, table: Any, ids: list[int], project_id: int) -> None:
    if not ids:
        return
    found = set((await session.scalars(select(table.c.id).where(table.c.id.in_(ids), table.c.project_id == project_id, table.c.active.is_(True)))).all())
    if found != set(ids):
        raise HTTPException(422, detail={"code": "CROSS_PROJECT_REFERENCE", "message": "Reference is missing, inactive, or belongs to another project"})


async def _replace_requirements(session: AsyncSession, work_type_id: int, body: WorkTypeCreate | WorkTypePatch, project_id: int) -> None:
    qualification_ids = body.qualification_ids
    equipment_ids = body.equipment_type_ids
    if qualification_ids is not None:
        await _validate_ids(session, qualifications, qualification_ids, project_id)
        await session.execute(delete(work_type_required_qualifications).where(work_type_required_qualifications.c.work_type_id == work_type_id))
        if qualification_ids:
            await session.execute(insert(work_type_required_qualifications), [{"work_type_id": work_type_id, "qualification_id": value} for value in qualification_ids])
    if equipment_ids is not None:
        await _validate_ids(session, equipment_types, equipment_ids, project_id)
        await session.execute(delete(work_type_required_equipment).where(work_type_required_equipment.c.work_type_id == work_type_id))
        if equipment_ids:
            await session.execute(insert(work_type_required_equipment), [{"work_type_id": work_type_id, "equipment_type_id": value} for value in equipment_ids])


async def _work_type_result(session: AsyncSession, row: Any) -> dict[str, Any]:
    qualification_ids = list((await session.scalars(select(work_type_required_qualifications.c.qualification_id).where(work_type_required_qualifications.c.work_type_id == row.id))).all())
    equipment_ids = list((await session.scalars(select(work_type_required_equipment.c.equipment_type_id).where(work_type_required_equipment.c.work_type_id == row.id))).all())
    return {**_dict(row), "qualification_ids": qualification_ids, "equipment_type_ids": equipment_ids}


@project_router.get("/work-types")
@inject
async def work_type_list(access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    _, project_id = await access.dispatcher()
    rows = (await session.execute(select(work_types).where(work_types.c.project_id == project_id).order_by(work_types.c.name))).mappings().all()
    return [await _work_type_result(session, row) for row in rows]


@project_router.post("/work-types", status_code=201)
@inject
async def work_type_create(body: WorkTypeCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    values = body.model_dump(exclude={"qualification_ids", "equipment_type_ids"})
    try:
        row = (await session.execute(insert(work_types).values(project_id=project_id, code=_code("WORK"), **values).returning(*work_types.c))).mappings().one()
        await _replace_requirements(session, row.id, body, project_id)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "NAME_EXISTS", "message": "Name already exists in project"}) from error
    return await _work_type_result(session, row)


@project_router.patch("/work-types/{item_id}")
@inject
async def work_type_patch(item_id: int, body: WorkTypePatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    await _belongs(session, work_types, item_id, project_id)
    values = body.model_dump(exclude_none=True, exclude={"qualification_ids", "equipment_type_ids"})
    try:
        row = (await session.execute(update(work_types).where(work_types.c.id == item_id).values(**values).returning(*work_types.c))).mappings().one()
        await _replace_requirements(session, item_id, body, project_id)
        await session.commit()
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "NAME_EXISTS", "message": "Name already exists in project"}) from error
    return await _work_type_result(session, row)


class EngineerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    active: bool = True
    transport_type: Literal["CAR", "NONE"]
    start_address: str = Field(min_length=1)
    start_latitude: float | None = None
    start_longitude: float | None = None
    qualification_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def coordinate_pair(self) -> "EngineerCreate":
        if (self.start_latitude is None) != (self.start_longitude is None):
            raise ValueError("coordinates must be provided together")
        return self


class EngineerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    active: bool | None = None
    transport_type: Literal["CAR", "NONE"] | None = None
    start_address: str | None = Field(default=None, min_length=1)
    start_latitude: float | None = None
    start_longitude: float | None = None
    qualification_ids: list[int] | None = None


async def _engineer_result(session: AsyncSession, row: Any) -> dict[str, Any]:
    qualification_ids = list((await session.scalars(select(engineer_qualifications.c.qualification_id).where(engineer_qualifications.c.engineer_id == row.id))).all())
    account = (await session.execute(select(users_table.c.username, users_table.c.is_active).where(users_table.c.engineer_id == row.id))).mappings().one_or_none()
    return {**_dict(row), "qualification_ids": qualification_ids, "access": None if account is None else {"login": account.username, "status": "ACTIVE" if account.is_active else "BLOCKED"}}


@project_router.get("/engineers")
@inject
async def engineer_list(access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    _, project_id = await access.dispatcher()
    rows = (await session.execute(select(engineers).where(engineers.c.project_id == project_id).order_by(engineers.c.name))).mappings().all()
    return [await _engineer_result(session, row) for row in rows]


@project_router.post("/engineers", status_code=201)
@inject
async def engineer_create(body: EngineerCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    await _validate_ids(session, qualifications, body.qualification_ids, project_id)
    values = body.model_dump(exclude={"qualification_ids"})
    row = (await session.execute(insert(engineers).values(project_id=project_id, internal_code=_code("ENG"), **values).returning(*engineers.c))).mappings().one()
    if body.qualification_ids:
        await session.execute(insert(engineer_qualifications), [{"engineer_id": row.id, "qualification_id": value} for value in body.qualification_ids])
    await session.commit()
    return await _engineer_result(session, row)


@project_router.get("/engineers/{engineer_id}")
@inject
async def engineer_get(engineer_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    row = await _belongs(session, engineers, engineer_id, project_id)
    result = await _engineer_result(session, row)
    schedules = (await session.execute(select(engineer_schedules).where(engineer_schedules.c.engineer_id == engineer_id).order_by(engineer_schedules.c.work_date))).mappings().all()
    result["schedule"] = [_dict(item, hidden=()) for item in schedules]
    return result


@project_router.patch("/engineers/{engineer_id}")
@inject
async def engineer_patch(engineer_id: int, body: EngineerPatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    await _belongs(session, engineers, engineer_id, project_id)
    if body.qualification_ids is not None:
        await _validate_ids(session, qualifications, body.qualification_ids, project_id)
        await session.execute(delete(engineer_qualifications).where(engineer_qualifications.c.engineer_id == engineer_id))
        if body.qualification_ids:
            await session.execute(insert(engineer_qualifications), [{"engineer_id": engineer_id, "qualification_id": value} for value in body.qualification_ids])
    values = body.model_dump(exclude_none=True, exclude={"qualification_ids"})
    if "start_address" in values and ("start_latitude" not in values or "start_longitude" not in values):
        values.update(start_latitude=None, start_longitude=None)
    values["updated_at"] = datetime.now(timezone.utc)
    row = (await session.execute(update(engineers).where(engineers.c.id == engineer_id).values(**values).returning(*engineers.c))).mappings().one()
    await session.commit()
    return await _engineer_result(session, row)


class ScheduleEntry(BaseModel):
    work_date: date
    working: bool = True
    shift_start: time | None = None
    shift_end: time | None = None

    @model_validator(mode="after")
    def valid_shift(self) -> "ScheduleEntry":
        if self.working and (self.shift_start is None or self.shift_end is None or self.shift_start >= self.shift_end):
            raise ValueError("working date requires shift_start < shift_end")
        return self


class SchedulePut(BaseModel):
    entries: list[ScheduleEntry] = Field(min_length=1, max_length=366)


@project_router.put("/engineers/{engineer_id}/schedule")
@inject
async def schedule_put(engineer_id: int, body: SchedulePut, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> list[dict[str, Any]]:
    _, project_id = await access.dispatcher()
    await _belongs(session, engineers, engineer_id, project_id)
    dates = [entry.work_date for entry in body.entries]
    if len(dates) != len(set(dates)):
        raise HTTPException(422, detail={"code": "DUPLICATE_DATE", "message": "Schedule dates must be unique"})
    await session.execute(delete(engineer_schedules).where(engineer_schedules.c.engineer_id == engineer_id, engineer_schedules.c.work_date.in_(dates)))
    working = [entry for entry in body.entries if entry.working]
    if working:
        await session.execute(insert(engineer_schedules), [{"engineer_id": engineer_id, "work_date": item.work_date, "shift_start": item.shift_start, "shift_end": item.shift_end} for item in working])
    await session.commit()
    rows = (await session.execute(select(engineer_schedules).where(engineer_schedules.c.engineer_id == engineer_id, engineer_schedules.c.work_date.in_(dates)).order_by(engineer_schedules.c.work_date))).mappings().all()
    return [_dict(row, hidden=()) for row in rows]


class AccessCreate(BaseModel):
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class AccessPasswordReset(BaseModel):
    password: str = Field(min_length=1, max_length=128)


async def _engineer_account(engineer_id: int, project_id: int, session: AsyncSession) -> Any:
    await _belongs(session, engineers, engineer_id, project_id)
    return (await session.execute(select(users_table).where(users_table.c.engineer_id == engineer_id))).mappings().one_or_none()


@project_router.post("/engineers/{engineer_id}/access", status_code=201)
@inject
async def engineer_access(engineer_id: int, body: AccessCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession], hasher: FromDishka[PasswordHasher]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    if await _engineer_account(engineer_id, project_id, session) is not None:
        raise HTTPException(409, detail={"code": "ACCESS_EXISTS", "message": "Engineer already has an account"})
    if await session.scalar(select(users_table.c.id).where(func.lower(users_table.c.username) == body.login.strip().lower())) is not None:
        raise HTTPException(409, detail={"code": "LOGIN_EXISTS", "message": "Login already exists"})
    row = (await session.execute(insert(users_table).values(id=uuid.uuid4(), username=body.login.strip(), password_hash=hasher.hash(RawPassword(body.password)), is_active=True, role=UserRoleEnum.ENGINEER, is_verified=True, project_id=project_id, engineer_id=engineer_id).returning(*users_table.c))).mappings().one()
    await session.commit()
    return {"id": str(row.id), "login": row.username, "status": "ACTIVE"}


@project_router.post("/engineers/{engineer_id}/reset-password")
@inject
async def engineer_password(engineer_id: int, body: AccessPasswordReset, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession], hasher: FromDishka[PasswordHasher]) -> dict[str, str]:
    _, project_id = await access.dispatcher()
    account = await _engineer_account(engineer_id, project_id, session)
    if account is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Engineer account not found"})
    await session.execute(update(users_table).where(users_table.c.id == account.id).values(password_hash=hasher.hash(RawPassword(body.password))))
    await session.execute(delete(sessions_table).where(sessions_table.c.user_id == account.id))
    await session.commit()
    return {"status": "PASSWORD_RESET"}


async def _set_engineer_access(engineer_id: int, active: bool, project_id: int, session: AsyncSession) -> dict[str, str]:
    account = await _engineer_account(engineer_id, project_id, session)
    if account is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Engineer account not found"})
    await session.execute(update(users_table).where(users_table.c.id == account.id).values(is_active=active))
    if not active:
        await session.execute(delete(sessions_table).where(sessions_table.c.user_id == account.id))
    await session.commit()
    return {"status": "ACTIVE" if active else "BLOCKED"}


@project_router.post("/engineers/{engineer_id}/access/block")
@inject
async def engineer_access_block(engineer_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, str]:
    _, project_id = await access.dispatcher()
    return await _set_engineer_access(engineer_id, False, project_id, session)


@project_router.post("/engineers/{engineer_id}/access/unblock")
@inject
async def engineer_access_unblock(engineer_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, str]:
    _, project_id = await access.dispatcher()
    return await _set_engineer_access(engineer_id, True, project_id, session)


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = Field(min_length=1, max_length=1000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    sla_date: date
    time_window_start: time | None = None
    time_window_end: time | None = None
    work_type_id: int

    @model_validator(mode="after")
    def validate_values(self) -> "JobCreate":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("coordinates must be provided together")
        if self.time_window_start and self.time_window_end and self.time_window_start > self.time_window_end:
            raise ValueError("time window cannot cross midnight")
        return self


class JobPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str | None = Field(default=None, min_length=1, max_length=1000)
    latitude: float | None = None
    longitude: float | None = None
    sla_date: date | None = None
    time_window_start: time | None = None
    time_window_end: time | None = None
    work_type_id: int | None = None


class JobStatusChange(BaseModel):
    status: Literal["NEW", "IN_PROGRESS", "COMPLETED", "CANCELLED"]
    reason: str | None = Field(default=None, max_length=1000)


async def _work_type_for_job(work_type_id: int, project_id: int, session: AsyncSession) -> Any:
    row = (await session.execute(select(work_types).where(work_types.c.id == work_type_id, work_types.c.project_id == project_id, work_types.c.active.is_(True)))).mappings().one_or_none()
    if row is None:
        raise HTTPException(422, detail={"code": "CROSS_PROJECT_REFERENCE", "message": "Active work type not found in project"})
    if not row.default_service_duration_min or row.default_service_duration_min <= 0:
        raise HTTPException(422, detail={"code": "MISSING_SERVICE_DURATION", "message": "Work type has no positive duration"})
    return row


async def _job_result(row: Any, session: AsyncSession) -> dict[str, Any]:
    history = (await session.execute(select(job_status_history).where(job_status_history.c.job_id == row.id).order_by(job_status_history.c.created_at))).mappings().all()
    assignment = (await session.execute(select(assignments.c.id, assignments.c.engineer_id, assignments.c.sequence, assignments.c.planned_start, assignments.c.planned_finish).select_from(assignments.join(plan_versions, assignments.c.plan_version_id == plan_versions.c.id).join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id)).where(assignments.c.job_id == row.id, assignments.c.active.is_(True)))).mappings().one_or_none()
    return {**_dict(row, hidden=("internal_code", "external_id", "address_hash", "geocoded_at")), "assignment": dict(assignment) if assignment else None, "status_history": [_dict(item, hidden=()) for item in history]}


@project_router.get("/jobs")
@inject
async def job_list(
    access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession],
    search: str | None = None, job_status: str | None = Query(default=None, alias="status"),
    sla_date: date | None = None, work_type_id: int | None = None,
    assigned: bool | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    query = select(jobs).where(jobs.c.project_id == project_id)
    if search:
        query = query.where(jobs.c.address.ilike(f"%{search}%"))
    if job_status:
        query = query.where(jobs.c.status == job_status)
    if sla_date:
        query = query.where(jobs.c.sla_date == sla_date)
    if work_type_id:
        query = query.where(jobs.c.work_type_id == work_type_id)
    if assigned is not None:
        current_assignment = select(assignments.c.job_id).join(plan_versions).join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id)
        query = query.where(jobs.c.id.in_(current_assignment) if assigned else ~jobs.c.id.in_(current_assignment))
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (await session.execute(query.order_by(jobs.c.sla_date, jobs.c.created_at, jobs.c.id).limit(limit).offset(offset))).mappings().all()
    return {"items": [await _job_result(row, session) for row in rows], "total": int(total or 0), "limit": limit, "offset": offset}


@project_router.post("/jobs", status_code=201)
@inject
async def job_create(body: JobCreate, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    work_type = await _work_type_for_job(body.work_type_id, project_id, session)
    values = body.model_dump()
    values.update(project_id=project_id, internal_code=_code("JOB"), external_id=None, status="NEW", service_duration_min=work_type.default_service_duration_min)
    row = (await session.execute(insert(jobs).values(**values).returning(*jobs.c))).mappings().one()
    await session.commit()
    return await _job_result(row, session)


@project_router.get("/jobs/{job_id}")
@inject
async def job_get(job_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    return await _job_result(await _belongs(session, jobs, job_id, project_id), session)


@project_router.patch("/jobs/{job_id}")
@inject
async def job_patch(job_id: int, body: JobPatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    current = await _belongs(session, jobs, job_id, project_id)
    if current.status != "NEW":
        raise HTTPException(409, detail={"code": "INVALID_STATUS_TRANSITION", "message": "Only NEW jobs may be edited"})
    published = await session.scalar(select(assignments.c.id).join(plan_versions).join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id).where(assignments.c.job_id == job_id))
    if published is not None:
        raise HTTPException(409, detail={"code": "JOB_PUBLISHED", "message": "Published job cannot be edited"})
    values = body.model_dump(exclude_unset=True)
    work_type_id = values.get("work_type_id", current.work_type_id)
    work_type = await _work_type_for_job(work_type_id, project_id, session)
    values["service_duration_min"] = work_type.default_service_duration_min
    if "address" in values and ("latitude" not in values or "longitude" not in values):
        values.update(latitude=None, longitude=None, address_hash=None, geocoded_at=None)
    final_start = values.get("time_window_start", current.time_window_start)
    final_end = values.get("time_window_end", current.time_window_end)
    if final_start is not None and final_end is not None and final_start > final_end:
        raise HTTPException(422, detail={"code": "INVALID_TIME_WINDOW", "message": "Time window cannot cross midnight"})
    final_latitude = values.get("latitude", current.latitude)
    final_longitude = values.get("longitude", current.longitude)
    if (final_latitude is None) != (final_longitude is None):
        raise HTTPException(422, detail={"code": "INVALID_COORDINATES", "message": "Coordinates must be provided together"})
    values["updated_at"] = datetime.now(timezone.utc)
    row = (await session.execute(update(jobs).where(jobs.c.id == job_id).values(**values).returning(*jobs.c))).mappings().one()
    await session.commit()
    return await _job_result(row, session)


@project_router.post("/jobs/{job_id}/status")
@inject
async def job_status_change(job_id: int, body: JobStatusChange, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    user, project_id = await access.dispatcher()
    return await change_job_status(session, user, job_id, body.status, project_id=project_id, reason=body.reason, dispatcher=True)


@project_router.get("/address-suggestions")
@inject
async def address_suggestions(access: FromDishka[ProjectAccess], config: FromDishka[PlanningServiceConfig], q: str = Query(min_length=3, max_length=300)) -> list[dict[str, Any]]:
    await access.dispatcher()
    try:
        async with httpx.AsyncClient(base_url=config.nominatim_url, timeout=config.geoservice_timeout_sec) as client:
            response = await client.get("/search", params={"q": q, "format": "jsonv2", "limit": 10, "countrycodes": "ru", "viewbox": config.nominatim_viewbox, "bounded": 1, "addressdetails": 1})
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(503, detail={"code": "ADDRESS_PROVIDER_UNAVAILABLE", "message": "Address suggestions are temporarily unavailable"}) from error
    result = []
    for item in response.json():
        address = item.get("address", {})
        if not any(address.get(key) for key in ("house_number", "building")):
            continue
        result.append({"display_name": item["display_name"], "latitude": float(item["lat"]), "longitude": float(item["lon"])})
    return result


class PublishRequest(BaseModel):
    confirm_unassigned: bool = False


class PlanningConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sla_overdue_base: int | None = Field(default=None, ge=0)
    sla_overdue_per_day: int | None = Field(default=None, ge=0)
    sla_today: int | None = Field(default=None, ge=0)
    sla_tomorrow: int | None = Field(default=None, ge=0)
    sla_2_3_days: int | None = Field(default=None, ge=0)
    sla_later: int | None = Field(default=None, ge=0)
    skill_one_engineer: int | None = Field(default=None, ge=0)
    skill_two_engineers: int | None = Field(default=None, ge=0)
    equipment_one_unit: int | None = Field(default=None, ge=0)
    equipment_two_units: int | None = Field(default=None, ge=0)
    window_30: int | None = Field(default=None, ge=0)
    window_60: int | None = Field(default=None, ge=0)
    window_120: int | None = Field(default=None, ge=0)
    travel_cost_per_minute: int | None = Field(default=None, ge=0)
    solver_time_limit_sec: int | None = Field(default=None, ge=1, le=600)
    max_jobs_per_run: int | None = Field(default=None, ge=1, le=1000)
    travel_provider: Literal["VALHALLA_LOCAL"] | None = None


@project_router.get("/planning-config")
@inject
async def planning_config_get(access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    row = (await session.execute(select(planning_config).where(planning_config.c.project_id == project_id, planning_config.c.active.is_(True)))).mappings().one_or_none()
    if row is None:
        raise HTTPException(409, detail={"code": "PLANNING_CONFIG_MISSING", "message": "Project has no active planning configuration"})
    return _dict(row, hidden=())


@project_router.patch("/planning-config")
@inject
async def planning_config_patch(body: PlanningConfigPatch, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    current = (await session.execute(select(planning_config).where(planning_config.c.project_id == project_id, planning_config.c.active.is_(True)).with_for_update())).mappings().one_or_none()
    if current is None:
        raise HTTPException(409, detail={"code": "PLANNING_CONFIG_MISSING", "message": "Project has no active planning configuration"})
    values = {key: value for key, value in dict(current).items() if key not in {"id", "created_at", "updated_at", "active"}}
    values.update(body.model_dump(exclude_none=True))
    values.update(version=current.version + 1, active=True)
    await session.execute(update(planning_config).where(planning_config.c.id == current.id).values(active=False, updated_at=datetime.now(timezone.utc)))
    row = (await session.execute(insert(planning_config).values(**values).returning(*planning_config.c))).mappings().one()
    await session.commit()
    return _dict(row, hidden=())


@project_router.get("/planning/readiness")
@inject
async def planning_readiness(planning_date: date, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    problems: list[dict[str, str]] = []
    if await session.scalar(select(planning_config.c.id).where(planning_config.c.project_id == project_id, planning_config.c.active.is_(True))) is None:
        problems.append({"code": "PLANNING_CONFIG_MISSING", "message": "Active planning configuration is missing"})
    if await session.scalar(select(work_types.c.id).where(work_types.c.project_id == project_id, work_types.c.active.is_(True)).limit(1)) is None:
        problems.append({"code": "WORK_TYPES_MISSING", "message": "Create at least one active work type"})
    eligible_engineer = await session.scalar(select(engineers.c.id).join(engineer_schedules, engineer_schedules.c.engineer_id == engineers.c.id).where(engineers.c.project_id == project_id, engineers.c.active.is_(True), engineer_schedules.c.work_date == planning_date).limit(1))
    if eligible_engineer is None:
        problems.append({"code": "ENGINEERS_MISSING", "message": "No active engineer has a shift on this date"})
    if await session.scalar(select(jobs.c.id).where(jobs.c.project_id == project_id, jobs.c.status == "NEW").limit(1)) is None:
        problems.append({"code": "JOBS_MISSING", "message": "No NEW jobs are available"})
    return {"planning_date": planning_date, "ready": not problems, "problems": problems}


@project_router.post("/planning/runs/{run_id}/publish", status_code=201)
@inject
async def publish_run(run_id: int, body: PublishRequest, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    user, project_id = await access.dispatcher()
    run = (await session.execute(select(planning_runs).where(planning_runs.c.id == run_id, planning_runs.c.project_id == project_id).with_for_update())).mappings().one_or_none()
    if run is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Planning run not found"})
    if run.status != "SUCCESS" or run.validation_errors:
        raise HTTPException(409, detail={"code": "PLANNING_RUN_NOT_PUBLISHABLE", "message": "Only a validated successful run can be published"})
    if run.unassigned_jobs_count and not body.confirm_unassigned:
        raise HTTPException(409, detail={"code": "UNASSIGNED_CONFIRMATION_REQUIRED", "message": "Confirm publication with unassigned jobs"})
    if await session.scalar(select(plan_versions.c.id).where(plan_versions.c.planning_run_id == run_id)) is not None:
        raise HTTPException(409, detail={"code": "ALREADY_PUBLISHED", "message": "Planning run is already published"})
    plan = (await session.execute(select(daily_plans).where(daily_plans.c.project_id == project_id, daily_plans.c.planning_date == run.planning_date).with_for_update())).mappings().one_or_none()
    if plan is None:
        try:
            plan_id = await session.scalar(insert(daily_plans).values(project_id=project_id, planning_date=run.planning_date).returning(daily_plans.c.id))
        except IntegrityError as error:
            await session.rollback()
            raise HTTPException(409, detail={"code": "CONCURRENT_MODIFICATION", "message": "Daily plan was published concurrently"}) from error
        current_version_id = None
        version_number = 1
    else:
        plan_id = plan.id
        current_version_id = plan.current_version_id
        version_number = int(await session.scalar(select(func.coalesce(func.max(plan_versions.c.version_number), 0)).where(plan_versions.c.daily_plan_id == plan_id))) + 1
    if current_version_id is not None:
        started = await session.scalar(select(jobs.c.id).join(assignments, assignments.c.job_id == jobs.c.id).where(assignments.c.plan_version_id == current_version_id, jobs.c.status.in_(["IN_PROGRESS", "COMPLETED"])).with_for_update().limit(1))
        if started is not None:
            raise HTTPException(409, detail={"code": "PLAN_ALREADY_STARTED", "message": "Published plan cannot be replaced after work started"})
        await session.execute(update(plan_versions).where(plan_versions.c.id == current_version_id).values(status="SUPERSEDED", superseded_at=datetime.now(timezone.utc)))
    try:
        version_id = await session.scalar(insert(plan_versions).values(daily_plan_id=plan_id, version_number=version_number, planning_run_id=run_id, status="PUBLISHED", published_by=user.id).returning(plan_versions.c.id))
    except IntegrityError as error:
        await session.rollback()
        raise HTTPException(409, detail={"code": "CONCURRENT_MODIFICATION", "message": "Planning run was published concurrently"}) from error
    route_rows = (await session.execute(select(planning_route_jobs, planning_routes.c.engineer_id).join(planning_routes, planning_route_jobs.c.planning_route_id == planning_routes.c.id).where(planning_route_jobs.c.planning_run_id == run_id).order_by(planning_routes.c.engineer_id, planning_route_jobs.c.sequence))).mappings().all()
    assigned_job_ids = [int(item.job_id) for item in route_rows]
    if assigned_job_ids:
        publishable_jobs = set(
            (
                await session.scalars(
                    select(jobs.c.id).where(
                        jobs.c.id.in_(assigned_job_ids),
                        jobs.c.project_id == project_id,
                        jobs.c.status == "NEW",
                    ).with_for_update()
                )
            ).all()
        )
        if publishable_jobs != set(assigned_job_ids):
            raise HTTPException(409, detail={"code": "CONCURRENT_MODIFICATION", "message": "Assigned jobs changed after the calculation"})
    source_jobs = {int(item["id"]): item for item in run.input_snapshot.get("jobs", [])}
    for item in route_rows:
        snapshot = source_jobs.get(int(item.job_id), {})
        await session.execute(insert(assignments).values(plan_version_id=version_id, job_id=item.job_id, engineer_id=item.engineer_id, sequence=item.sequence, planned_start=item.planned_start, planned_finish=item.planned_finish, route_data={"planned_arrival": item.planned_arrival.isoformat(), "travel_from_previous_min": item.travel_from_previous_min, "waiting_before_job_min": item.waiting_before_job_min}, requirement_snapshot={"required_qualifications": snapshot.get("required_qualifications", []), "required_equipment": snapshot.get("required_equipment", []), "required_transport": snapshot.get("required_transport")}))
    await session.execute(update(daily_plans).where(daily_plans.c.id == plan_id).values(current_version_id=version_id, updated_at=datetime.now(timezone.utc)))
    await session.commit()
    return {"daily_plan_id": int(plan_id), "plan_version_id": int(version_id), "version_number": version_number, "status": "PUBLISHED", "assignments_count": len(route_rows)}


def _plan_state(statuses: list[str], version_status: str) -> str:
    if version_status == "SUPERSEDED":
        return "SUPERSEDED"
    if not statuses:
        return "PUBLISHED"
    if all(value in {"COMPLETED", "CANCELLED"} for value in statuses):
        return "COMPLETED"
    if any(value in {"IN_PROGRESS", "COMPLETED"} for value in statuses):
        return "IN_PROGRESS"
    return "PUBLISHED"


@project_router.get("/daily-plans/{planning_date}")
@inject
async def daily_plan_get(planning_date: date, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, project_id = await access.dispatcher()
    plan = (await session.execute(select(daily_plans, plan_versions).join(plan_versions, daily_plans.c.current_version_id == plan_versions.c.id).where(daily_plans.c.project_id == project_id, daily_plans.c.planning_date == planning_date))).mappings().one_or_none()
    if plan is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Published daily plan not found"})
    rows = (await session.execute(select(assignments, jobs.c.status, jobs.c.address, jobs.c.work_type_id).join(jobs, assignments.c.job_id == jobs.c.id).where(assignments.c.plan_version_id == plan.current_version_id, assignments.c.active.is_(True)).order_by(assignments.c.engineer_id, assignments.c.sequence))).mappings().all()
    statuses = [row.status for row in rows]
    return {"id": plan.id, "project_id": project_id, "planning_date": planning_date, "version": plan.version_number, "planning_run_id": plan.planning_run_id, "status": _plan_state(statuses, plan.status), "assignments": [_dict(row, hidden=()) for row in rows]}
