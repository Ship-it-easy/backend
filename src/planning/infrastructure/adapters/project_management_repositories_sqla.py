import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.session import sessions_table
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.application.errors import (
    ConflictError,
    InvalidJobStatusError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
)
from planning.application.interfaces.project_management_repositories import (
    EngineerAccountRepository,
    EngineerManagementRepository,
    ProjectCatalogRepository,
    ProjectJobsRepository,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    job_status_history,
    jobs,
    plan_versions,
    qualifications,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _dict(
    row: Any,
    *,
    hidden: tuple[str, ...] = ("internal_code", "code"),
) -> dict[str, Any]:
    return {key: value for key, value in dict(row).items() if key not in hidden}


async def _belongs(
    session: AsyncSession,
    table: Any,
    item_id: int,
    project_id: int,
) -> Any:
    row = (
        (
            await session.execute(
                select(table).where(
                    table.c.id == item_id, table.c.project_id == project_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ObjectNotFoundError("Object not found")
    return row


async def _validate_ids(
    session: AsyncSession,
    table: Any,
    ids: list[int],
    project_id: int,
) -> None:
    if not ids:
        return
    found = set(
        (
            await session.scalars(
                select(table.c.id).where(
                    table.c.id.in_(ids),
                    table.c.project_id == project_id,
                    table.c.active.is_(True),
                )
            )
        ).all()
    )
    if found != set(ids):
        raise InvalidPlanningRequest(
            "Reference is missing, inactive, or belongs to another project",
            code="CROSS_PROJECT_REFERENCE",
        )


class SqlaProjectCatalogRepository(ProjectCatalogRepository):
    _tables = {"qualification": qualifications, "equipment": equipment_types}

    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_catalog(self, project_id: int, kind: str) -> list[dict[str, Any]]:
        table = self._tables[kind]
        rows = (
            (
                await self._session.execute(
                    select(table)
                    .where(table.c.project_id == project_id)
                    .order_by(table.c.name)
                )
            )
            .mappings()
            .all()
        )
        return [_dict(row) for row in rows]

    async def create_catalog(
        self, project_id: int, kind: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        table = self._tables[kind]
        values = {
            **values,
            "code": _code("QUAL" if kind == "qualification" else "EQUIP"),
        }
        try:
            row = (
                (
                    await self._session.execute(
                        insert(table)
                        .values(project_id=project_id, **values)
                        .returning(*table.c)
                    )
                )
                .mappings()
                .one()
            )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "Name already exists in project", code="NAME_EXISTS"
            ) from error
        return _dict(row)

    async def update_catalog(
        self,
        project_id: int,
        kind: str,
        item_id: int,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        table = self._tables[kind]
        await _belongs(self._session, table, item_id, project_id)
        try:
            row = (
                (
                    await self._session.execute(
                        update(table)
                        .where(table.c.id == item_id)
                        .values(**values)
                        .returning(*table.c)
                    )
                )
                .mappings()
                .one()
            )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "Name already exists in project", code="NAME_EXISTS"
            ) from error
        return _dict(row)

    async def clear_equipment(self, project_id: int, item_id: int) -> dict[str, Any]:
        await _belongs(self._session, equipment_types, item_id, project_id)
        row = (
            (
                await self._session.execute(
                    update(equipment_types)
                    .where(equipment_types.c.id == item_id)
                    .values(available_units=0)
                    .returning(*equipment_types.c)
                )
            )
            .mappings()
            .one()
        )
        await self._session.commit()
        return _dict(row)

    async def list_work_types(self, project_id: int) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    select(work_types)
                    .where(work_types.c.project_id == project_id)
                    .order_by(work_types.c.name)
                )
            )
            .mappings()
            .all()
        )
        return [await self._work_type_result(row) for row in rows]

    async def create_work_type(
        self,
        project_id: int,
        values: dict[str, Any],
        qualification_ids: list[int],
        equipment_type_ids: list[int],
    ) -> dict[str, Any]:
        await self._validate_requirements(
            project_id, qualification_ids, equipment_type_ids
        )
        try:
            row = (
                (
                    await self._session.execute(
                        insert(work_types)
                        .values(project_id=project_id, code=_code("WORK"), **values)
                        .returning(*work_types.c)
                    )
                )
                .mappings()
                .one()
            )
            await self._replace_requirements(
                row.id, qualification_ids, equipment_type_ids
            )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "Name already exists in project", code="NAME_EXISTS"
            ) from error
        return await self._work_type_result(row)

    async def update_work_type(
        self,
        project_id: int,
        item_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
        equipment_type_ids: list[int] | None,
    ) -> dict[str, Any]:
        await _belongs(self._session, work_types, item_id, project_id)
        await self._validate_requirements(
            project_id,
            qualification_ids or [],
            equipment_type_ids or [],
        )
        try:
            row = (
                (
                    await self._session.execute(
                        update(work_types)
                        .where(work_types.c.id == item_id)
                        .values(**values)
                        .returning(*work_types.c)
                    )
                )
                .mappings()
                .one()
            )
            await self._replace_requirements(
                item_id, qualification_ids, equipment_type_ids
            )
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise ConflictError(
                "Name already exists in project", code="NAME_EXISTS"
            ) from error
        return await self._work_type_result(row)

    async def _validate_requirements(
        self,
        project_id: int,
        qualification_ids: list[int],
        equipment_type_ids: list[int],
    ) -> None:
        await _validate_ids(
            self._session, qualifications, qualification_ids, project_id
        )
        await _validate_ids(
            self._session, equipment_types, equipment_type_ids, project_id
        )

    async def _replace_requirements(
        self,
        work_type_id: int,
        qualification_ids: list[int] | None,
        equipment_type_ids: list[int] | None,
    ) -> None:
        if qualification_ids is not None:
            await self._session.execute(
                delete(work_type_required_qualifications).where(
                    work_type_required_qualifications.c.work_type_id == work_type_id
                )
            )
            if qualification_ids:
                await self._session.execute(
                    insert(work_type_required_qualifications),
                    [
                        {"work_type_id": work_type_id, "qualification_id": item}
                        for item in qualification_ids
                    ],
                )
        if equipment_type_ids is not None:
            await self._session.execute(
                delete(work_type_required_equipment).where(
                    work_type_required_equipment.c.work_type_id == work_type_id
                )
            )
            if equipment_type_ids:
                await self._session.execute(
                    insert(work_type_required_equipment),
                    [
                        {"work_type_id": work_type_id, "equipment_type_id": item}
                        for item in equipment_type_ids
                    ],
                )

    async def _work_type_result(self, row: Any) -> dict[str, Any]:
        qualification_ids = list(
            (
                await self._session.scalars(
                    select(work_type_required_qualifications.c.qualification_id).where(
                        work_type_required_qualifications.c.work_type_id == row.id
                    )
                )
            ).all()
        )
        equipment_ids = list(
            (
                await self._session.scalars(
                    select(work_type_required_equipment.c.equipment_type_id).where(
                        work_type_required_equipment.c.work_type_id == row.id
                    )
                )
            ).all()
        )
        return {
            **_dict(row),
            "qualification_ids": qualification_ids,
            "equipment_type_ids": equipment_ids,
        }


class SqlaEngineerManagementRepository(EngineerManagementRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_engineers(self, project_id: int) -> list[dict[str, Any]]:
        rows = (
            (
                await self._session.execute(
                    select(engineers)
                    .where(engineers.c.project_id == project_id)
                    .order_by(engineers.c.name)
                )
            )
            .mappings()
            .all()
        )
        return [await self._result(row) for row in rows]

    async def create_engineer(
        self,
        project_id: int,
        values: dict[str, Any],
        qualification_ids: list[int],
    ) -> dict[str, Any]:
        await _validate_ids(
            self._session, qualifications, qualification_ids, project_id
        )
        row = (
            (
                await self._session.execute(
                    insert(engineers)
                    .values(project_id=project_id, internal_code=_code("ENG"), **values)
                    .returning(*engineers.c)
                )
            )
            .mappings()
            .one()
        )
        if qualification_ids:
            await self._session.execute(
                insert(engineer_qualifications),
                [
                    {"engineer_id": row.id, "qualification_id": item}
                    for item in qualification_ids
                ],
            )
        await self._session.commit()
        return await self._result(row)

    async def get_engineer(self, project_id: int, engineer_id: int) -> dict[str, Any]:
        row = await _belongs(self._session, engineers, engineer_id, project_id)
        result = await self._result(row)
        schedules = (
            (
                await self._session.execute(
                    select(engineer_schedules)
                    .where(engineer_schedules.c.engineer_id == engineer_id)
                    .order_by(engineer_schedules.c.work_date)
                )
            )
            .mappings()
            .all()
        )
        result["schedule"] = [_dict(item, hidden=()) for item in schedules]
        return result

    async def update_engineer(
        self,
        project_id: int,
        engineer_id: int,
        values: dict[str, Any],
        qualification_ids: list[int] | None,
    ) -> dict[str, Any]:
        await _belongs(self._session, engineers, engineer_id, project_id)
        if qualification_ids is not None:
            await _validate_ids(
                self._session, qualifications, qualification_ids, project_id
            )
            await self._session.execute(
                delete(engineer_qualifications).where(
                    engineer_qualifications.c.engineer_id == engineer_id
                )
            )
            if qualification_ids:
                await self._session.execute(
                    insert(engineer_qualifications),
                    [
                        {"engineer_id": engineer_id, "qualification_id": item}
                        for item in qualification_ids
                    ],
                )
        row = (
            (
                await self._session.execute(
                    update(engineers)
                    .where(engineers.c.id == engineer_id)
                    .values(**values, updated_at=datetime.now(timezone.utc))
                    .returning(*engineers.c)
                )
            )
            .mappings()
            .one()
        )
        await self._session.commit()
        return await self._result(row)

    async def replace_schedule(
        self,
        project_id: int,
        engineer_id: int,
        entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        await _belongs(self._session, engineers, engineer_id, project_id)
        dates = [entry["work_date"] for entry in entries]
        await self._session.execute(
            delete(engineer_schedules).where(
                engineer_schedules.c.engineer_id == engineer_id,
                engineer_schedules.c.work_date.in_(dates),
            )
        )
        working = [entry for entry in entries if entry["working"]]
        if working:
            await self._session.execute(
                insert(engineer_schedules),
                [
                    {
                        "engineer_id": engineer_id,
                        "work_date": item["work_date"],
                        "shift_start": item["shift_start"],
                        "shift_end": item["shift_end"],
                    }
                    for item in working
                ],
            )
        await self._session.commit()
        rows = (
            (
                await self._session.execute(
                    select(engineer_schedules)
                    .where(
                        engineer_schedules.c.engineer_id == engineer_id,
                        engineer_schedules.c.work_date.in_(dates),
                    )
                    .order_by(engineer_schedules.c.work_date)
                )
            )
            .mappings()
            .all()
        )
        return [_dict(row, hidden=()) for row in rows]

    async def _result(self, row: Any) -> dict[str, Any]:
        qualification_ids = list(
            (
                await self._session.scalars(
                    select(engineer_qualifications.c.qualification_id).where(
                        engineer_qualifications.c.engineer_id == row.id
                    )
                )
            ).all()
        )
        account = (
            (
                await self._session.execute(
                    select(users_table.c.username, users_table.c.is_active).where(
                        users_table.c.engineer_id == row.id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return {
            **_dict(row),
            "qualification_ids": qualification_ids,
            "access": None
            if account is None
            else {
                "login": account.username,
                "status": "ACTIVE" if account.is_active else "BLOCKED",
            },
        }


class SqlaEngineerAccountRepository(EngineerAccountRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_account(
        self,
        project_id: int,
        engineer_id: int,
        login: str,
        password_hash: str,
    ) -> dict[str, Any]:
        account = await self._account(project_id, engineer_id)
        if account is not None:
            raise ConflictError("Engineer already has an account", code="ACCESS_EXISTS")
        exists = await self._session.scalar(
            select(users_table.c.id).where(
                func.lower(users_table.c.username) == login.lower()
            )
        )
        if exists is not None:
            raise ConflictError("Login already exists", code="LOGIN_EXISTS")
        row = (
            (
                await self._session.execute(
                    insert(users_table)
                    .values(
                        id=uuid.uuid4(),
                        username=login,
                        password_hash=password_hash,
                        is_active=True,
                        role=UserRoleEnum.ENGINEER,
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
        await self._session.commit()
        return {"id": str(row.id), "login": row.username, "status": "ACTIVE"}

    async def reset_password(
        self, project_id: int, engineer_id: int, password_hash: str
    ) -> dict[str, str]:
        account = await self._required_account(project_id, engineer_id)
        await self._session.execute(
            update(users_table)
            .where(users_table.c.id == account.id)
            .values(password_hash=password_hash)
        )
        await self._session.execute(
            delete(sessions_table).where(sessions_table.c.user_id == account.id)
        )
        await self._session.commit()
        return {"status": "PASSWORD_RESET"}

    async def set_active(
        self, project_id: int, engineer_id: int, active: bool
    ) -> dict[str, str]:
        account = await self._required_account(project_id, engineer_id)
        await self._session.execute(
            update(users_table)
            .where(users_table.c.id == account.id)
            .values(is_active=active)
        )
        if not active:
            await self._session.execute(
                delete(sessions_table).where(sessions_table.c.user_id == account.id)
            )
        await self._session.commit()
        return {"status": "ACTIVE" if active else "BLOCKED"}

    async def _account(self, project_id: int, engineer_id: int) -> Any:
        await _belongs(self._session, engineers, engineer_id, project_id)
        return (
            (
                await self._session.execute(
                    select(users_table).where(users_table.c.engineer_id == engineer_id)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _required_account(self, project_id: int, engineer_id: int) -> Any:
        account = await self._account(project_id, engineer_id)
        if account is None:
            raise ObjectNotFoundError("Engineer account not found")
        return account


class SqlaProjectJobsRepository(ProjectJobsRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_jobs(
        self, project_id: int, filters: dict[str, Any]
    ) -> dict[str, Any]:
        query = select(jobs).where(jobs.c.project_id == project_id)
        if filters.get("search"):
            query = query.where(jobs.c.address.ilike(f"%{filters['search']}%"))
        if filters.get("status"):
            query = query.where(jobs.c.status == filters["status"])
        if filters.get("sla_date"):
            query = query.where(jobs.c.sla_date == filters["sla_date"])
        if filters.get("work_type_id"):
            query = query.where(jobs.c.work_type_id == filters["work_type_id"])
        if filters.get("assigned") is not None:
            current = (
                select(assignments.c.job_id)
                .join(plan_versions)
                .join(
                    daily_plans,
                    daily_plans.c.current_version_id == plan_versions.c.id,
                )
            )
            query = query.where(
                jobs.c.id.in_(current)
                if filters["assigned"]
                else ~jobs.c.id.in_(current)
            )
        total = await self._session.scalar(
            select(func.count()).select_from(query.subquery())
        )
        rows = (
            (
                await self._session.execute(
                    query.order_by(jobs.c.sla_date, jobs.c.created_at, jobs.c.id)
                    .limit(filters["limit"])
                    .offset(filters["offset"])
                )
            )
            .mappings()
            .all()
        )
        return {
            "items": [await self._result(row) for row in rows],
            "total": int(total or 0),
            "limit": filters["limit"],
            "offset": filters["offset"],
        }

    async def create_job(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        work_type = await self._work_type(values["work_type_id"], project_id)
        values.update(
            project_id=project_id,
            internal_code=_code("JOB"),
            external_id=None,
            status="NEW",
            service_duration_min=work_type.default_service_duration_min,
        )
        row = (
            (
                await self._session.execute(
                    insert(jobs).values(**values).returning(*jobs.c)
                )
            )
            .mappings()
            .one()
        )
        await self._session.commit()
        return await self._result(row)

    async def get_job(self, project_id: int, job_id: int) -> dict[str, Any]:
        return await self._result(
            await _belongs(self._session, jobs, job_id, project_id)
        )

    async def update_job(
        self, project_id: int, job_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        current = await _belongs(self._session, jobs, job_id, project_id)
        if current.status != "NEW":
            raise InvalidJobStatusError("Only NEW jobs may be edited")
        published = await self._session.scalar(
            select(assignments.c.id)
            .join(plan_versions)
            .join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id)
            .where(assignments.c.job_id == job_id)
        )
        if published is not None:
            raise ConflictError("Published job cannot be edited", code="JOB_PUBLISHED")
        work_type = await self._work_type(
            values.get("work_type_id", current.work_type_id), project_id
        )
        values["service_duration_min"] = work_type.default_service_duration_min
        if "address" in values and (
            "latitude" not in values or "longitude" not in values
        ):
            values.update(
                latitude=None, longitude=None, address_hash=None, geocoded_at=None
            )
        final_start = values.get("time_window_start", current.time_window_start)
        final_end = values.get("time_window_end", current.time_window_end)
        if (
            final_start is not None
            and final_end is not None
            and final_start > final_end
        ):
            raise InvalidPlanningRequest(
                "Time window cannot cross midnight", code="INVALID_TIME_WINDOW"
            )
        final_latitude = values.get("latitude", current.latitude)
        final_longitude = values.get("longitude", current.longitude)
        if (final_latitude is None) != (final_longitude is None):
            raise InvalidPlanningRequest(
                "Coordinates must be provided together", code="INVALID_COORDINATES"
            )
        row = (
            (
                await self._session.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(**values, updated_at=datetime.now(timezone.utc))
                    .returning(*jobs.c)
                )
            )
            .mappings()
            .one()
        )
        await self._session.commit()
        return await self._result(row)

    async def _work_type(self, work_type_id: int, project_id: int) -> Any:
        row = (
            (
                await self._session.execute(
                    select(work_types).where(
                        work_types.c.id == work_type_id,
                        work_types.c.project_id == project_id,
                        work_types.c.active.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise InvalidPlanningRequest(
                "Active work type not found in project", code="CROSS_PROJECT_REFERENCE"
            )
        if (
            not row.default_service_duration_min
            or row.default_service_duration_min <= 0
        ):
            raise InvalidPlanningRequest(
                "Work type has no positive duration", code="MISSING_SERVICE_DURATION"
            )
        return row

    async def _result(self, row: Any) -> dict[str, Any]:
        history = (
            (
                await self._session.execute(
                    select(job_status_history)
                    .where(job_status_history.c.job_id == row.id)
                    .order_by(job_status_history.c.created_at)
                )
            )
            .mappings()
            .all()
        )
        assignment = (
            (
                await self._session.execute(
                    select(
                        assignments.c.id,
                        assignments.c.engineer_id,
                        assignments.c.sequence,
                        assignments.c.planned_start,
                        assignments.c.planned_finish,
                    )
                    .select_from(
                        assignments.join(plan_versions).join(
                            daily_plans,
                            daily_plans.c.current_version_id == plan_versions.c.id,
                        )
                    )
                    .where(
                        assignments.c.job_id == row.id,
                        assignments.c.active.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return {
            **_dict(
                row,
                hidden=("internal_code", "external_id", "address_hash", "geocoded_at"),
            ),
            "assignment": dict(assignment) if assignment else None,
            "status_history": [_dict(item, hidden=()) for item in history],
        }
