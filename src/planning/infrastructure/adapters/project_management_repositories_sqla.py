import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.domain.user_role import UserRoleEnum
from auth.infrastructure.persistence_sqla.mappings.session import sessions_table
from auth.infrastructure.persistence_sqla.mappings.user import users_table
from planning.application.errors import (
    ConflictError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
)
from planning.application.interfaces.project_management_repositories import (
    EngineerAccountRepository,
    EngineerManagementRepository,
    ProjectCatalogRepository,
    ProjectJobsRepository,
)
from planning.application.management_dto import ProjectJobEditState, WorkTypeEditState
from planning.infrastructure.persistence_sqla.mappings.tables import (
    engineer_availability_events,
    engineer_qualifications,
    engineer_schedules,
    engineers,
    equipment_types,
    job_planning_state,
    job_status_history,
    jobs,
    planning_config,
    planning_events,
    project_plan_assignments,
    project_plan_versions,
    projects,
    qualifications,
    work_type_required_equipment,
    work_type_required_qualifications,
    work_types,
)


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def _constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None)


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
        actor_user_id: Any,
    ) -> dict[str, Any]:
        previous_engineer = await _belongs(
            self._session, engineers, engineer_id, project_id
        )
        active_changed = "active" in values and bool(values["active"]) != bool(
            previous_engineer.active
        )
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
        event_id = None
        if active_changed:
            current_version_id = await self._session.scalar(
                select(project_plan_versions.c.id).where(
                    project_plan_versions.c.project_id == project_id,
                    project_plan_versions.c.is_current.is_(True),
                )
            )
            timezone_name = await self._session.scalar(
                select(projects.c.planning_timezone).where(projects.c.id == project_id)
            )
            today = (
                datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
            )
            maximum_days = int(
                await self._session.scalar(
                    select(planning_config.c.batch_maximum_horizon_days).where(
                        planning_config.c.project_id == project_id,
                        planning_config.c.active.is_(True),
                    )
                )
                or 30
            )
            maximum_date = today + timedelta(days=maximum_days - 1)
            event_type = (
                "ENGINEER_AVAILABILITY_RESTORED"
                if bool(values["active"])
                else "ENGINEER_AVAILABILITY_LOST"
            )
            affected_job_ids: list[int] = []
            affected_dates: list[Any] = []
            if current_version_id is not None and not bool(values["active"]):
                assignment_rows = (
                    (
                        await self._session.execute(
                            select(
                                project_plan_assignments.c.job_id,
                                project_plan_assignments.c.planning_date,
                            )
                            .join(
                                jobs,
                                jobs.c.id == project_plan_assignments.c.job_id,
                            )
                            .where(
                                project_plan_assignments.c.plan_version_id
                                == current_version_id,
                                project_plan_assignments.c.engineer_id == engineer_id,
                                project_plan_assignments.c.planning_date.between(
                                    today, maximum_date
                                ),
                                jobs.c.status.in_(["NEW", "IN_PROGRESS"]),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                affected_job_ids = sorted(
                    {int(item.job_id) for item in assignment_rows}
                )
                affected_dates = sorted(
                    {item.planning_date for item in assignment_rows}
                )
            elif current_version_id is not None:
                affected_dates = list(
                    (
                        await self._session.scalars(
                            select(engineer_schedules.c.work_date).where(
                                engineer_schedules.c.engineer_id == engineer_id,
                                engineer_schedules.c.work_date.between(
                                    today, maximum_date
                                ),
                            )
                        )
                    ).all()
                )
            if affected_dates:
                for work_date in affected_dates:
                    await self._session.execute(
                        insert(engineer_availability_events).values(
                            project_id=project_id,
                            engineer_id=engineer_id,
                            change_type=event_type,
                            effective_date=work_date,
                            old_interval={"active": bool(previous_engineer.active)},
                            new_interval={"active": bool(values["active"])},
                            actor_user_id=actor_user_id,
                        )
                    )
                date_key = "restored_dates" if bool(values["active"]) else "lost_dates"
                event_id = int(
                    await self._session.scalar(
                        insert(planning_events)
                        .values(
                            project_id=project_id,
                            event_type=event_type,
                            job_ids=affected_job_ids,
                            engineer_ids=[engineer_id],
                            initiator="USER",
                            actor_user_id=actor_user_id,
                            idempotency_key=(
                                f"engineer-active:{engineer_id}:{uuid.uuid4().hex}"
                            ),
                            state="PENDING",
                            event_payload={
                                date_key: [
                                    value.isoformat() for value in affected_dates
                                ]
                            },
                        )
                        .returning(planning_events.c.id)
                    )
                )
        await self._session.commit()
        result = await self._result(row)
        result["planning_event_id"] = event_id
        result["planning_event_state"] = "PENDING" if event_id else None
        return result

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

    async def replace_schedule_with_event(
        self,
        project_id: int,
        engineer_id: int,
        entries: list[dict[str, Any]],
        actor_user_id: Any,
    ) -> dict[str, Any]:
        engineer = await _belongs(self._session, engineers, engineer_id, project_id)
        dates = [entry["work_date"] for entry in entries]
        previous_rows = (
            (
                await self._session.execute(
                    select(engineer_schedules).where(
                        engineer_schedules.c.engineer_id == engineer_id,
                        engineer_schedules.c.work_date.in_(dates),
                    )
                )
            )
            .mappings()
            .all()
        )
        previous = {
            row.work_date: {
                "shift_start": row.shift_start.isoformat(),
                "shift_end": row.shift_end.isoformat(),
            }
            for row in previous_rows
        }
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
        current_version_id = await self._session.scalar(
            select(project_plan_versions.c.id).where(
                project_plan_versions.c.project_id == project_id,
                project_plan_versions.c.is_current.is_(True),
            )
        )
        timezone_name = await self._session.scalar(
            select(projects.c.planning_timezone).where(projects.c.id == project_id)
        )
        project_today = (
            datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        )
        maximum_days = int(
            await self._session.scalar(
                select(planning_config.c.batch_maximum_horizon_days).where(
                    planning_config.c.project_id == project_id,
                    planning_config.c.active.is_(True),
                )
            )
            or 30
        )
        maximum_date = project_today + timedelta(days=maximum_days - 1)
        assignments_by_date: dict[Any, list[Any]] = {}
        if current_version_id is not None:
            assignment_rows = (
                (
                    await self._session.execute(
                        select(project_plan_assignments)
                        .join(
                            jobs,
                            jobs.c.id == project_plan_assignments.c.job_id,
                        )
                        .where(
                            project_plan_assignments.c.plan_version_id
                            == current_version_id,
                            project_plan_assignments.c.engineer_id == engineer_id,
                            project_plan_assignments.c.planning_date.in_(dates),
                            jobs.c.status.in_(["NEW", "IN_PROGRESS"]),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for row in assignment_rows:
                assignments_by_date.setdefault(row.planning_date, []).append(row)
        lost_dates: list[Any] = []
        restored_dates: list[Any] = []
        affected_job_ids: set[int] = set()
        for entry in entries:
            work_date = entry["work_date"]
            old = previous.get(work_date)
            new = (
                {
                    "shift_start": entry["shift_start"].isoformat(),
                    "shift_end": entry["shift_end"].isoformat(),
                }
                if entry["working"]
                else None
            )
            old_start = time.fromisoformat(old["shift_start"]) if old else None
            old_end = time.fromisoformat(old["shift_end"]) if old else None
            new_start = entry.get("shift_start") if entry["working"] else None
            new_end = entry.get("shift_end") if entry["working"] else None
            shrunk = old is not None and (
                new is None or new_start > old_start or new_end < old_end
            )
            expanded = new is not None and (
                old is None or new_start < old_start or new_end > old_end
            )
            invalid = []
            inside_published_horizon = (
                current_version_id is not None
                and project_today <= work_date <= maximum_date
            )
            if shrunk and inside_published_horizon:
                for assignment in assignments_by_date.get(work_date, []):
                    local_start = (
                        assignment.planned_start.astimezone(
                            ZoneInfo(timezone_name)
                        )
                        .time()
                        .replace(tzinfo=None)
                    )
                    local_finish = (
                        assignment.planned_finish.astimezone(
                            ZoneInfo(timezone_name)
                        )
                        .time()
                        .replace(tzinfo=None)
                    )
                    if new is None or local_start < new_start or local_finish > new_end:
                        invalid.append(int(assignment.job_id))
            change_type = None
            if invalid:
                lost_dates.append(work_date)
                affected_job_ids.update(invalid)
                change_type = "ENGINEER_AVAILABILITY_LOST"
            elif expanded and inside_published_horizon and bool(engineer.active):
                restored_dates.append(work_date)
                change_type = "ENGINEER_AVAILABILITY_RESTORED"
            if change_type:
                await self._session.execute(
                    insert(engineer_availability_events).values(
                        project_id=project_id,
                        engineer_id=engineer_id,
                        change_type=change_type,
                        effective_date=work_date,
                        old_interval=old,
                        new_interval=new,
                        actor_user_id=actor_user_id,
                    )
                )
        event_id = None
        event_type = (
            "ENGINEER_AVAILABILITY_LOST"
            if lost_dates
            else "ENGINEER_AVAILABILITY_RESTORED"
            if restored_dates
            else None
        )
        if event_type:
            event_id = int(
                await self._session.scalar(
                    insert(planning_events)
                    .values(
                        project_id=project_id,
                        event_type=event_type,
                        job_ids=sorted(affected_job_ids),
                        engineer_ids=[engineer_id],
                        initiator="USER",
                        actor_user_id=actor_user_id,
                        idempotency_key=f"availability:{engineer_id}:{uuid.uuid4().hex}",
                        state="PENDING",
                        event_payload={
                            "lost_dates": [value.isoformat() for value in lost_dates],
                            "restored_dates": [
                                value.isoformat() for value in restored_dates
                            ],
                        },
                    )
                    .returning(planning_events.c.id)
                )
            )
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
        return {
            "schedule": [_dict(row, hidden=()) for row in rows],
            "planning_event_id": event_id,
            "planning_event_state": "PENDING" if event_id else None,
            "affected_job_ids": sorted(affected_job_ids),
        }

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

    async def engineer_belongs_to_project(
        self, project_id: int, engineer_id: int
    ) -> bool:
        return (
            await self._session.scalar(
                select(engineers.c.id).where(
                    engineers.c.id == engineer_id,
                    engineers.c.project_id == project_id,
                )
            )
            is not None
        )

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
        except IntegrityError as error:
            await self._session.rollback()
            constraint = _constraint_name(error)
            if constraint == "uq_users_username_ci":
                raise ConflictError(
                    "Login already exists", code="LOGIN_EXISTS"
                ) from error
            if constraint in {"users_engineer_id_key", "uq_users_engineer_id"}:
                raise ConflictError(
                    "Engineer already has an account", code="ACCESS_EXISTS"
                ) from error
            raise
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
        self,
        project_id: int,
        engineer_id: int,
        active: bool,
        actor_user_id: Any,
    ) -> dict[str, Any]:
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
        availability = await SqlaEngineerManagementRepository(
            self._session
        ).update_engineer(
            project_id,
            engineer_id,
            {"active": active},
            None,
            actor_user_id,
        )
        return {
            "status": "ACTIVE" if active else "BLOCKED",
            "planning_event_id": availability.get("planning_event_id"),
            "planning_event_state": availability.get("planning_event_state"),
        }

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
        query = (
            select(jobs, work_types.c.priority.label("priority"))
            .join(work_types, work_types.c.id == jobs.c.work_type_id)
            .where(jobs.c.project_id == project_id)
        )
        if filters.get("search"):
            query = query.where(jobs.c.address.ilike(f"%{filters['search']}%"))
        if filters.get("status"):
            query = query.where(jobs.c.status == filters["status"])
        if filters.get("sla_date"):
            query = query.where(jobs.c.sla_date == filters["sla_date"])
        if filters.get("work_type_id"):
            query = query.where(jobs.c.work_type_id == filters["work_type_id"])
        if filters.get("import_batch_id"):
            query = query.where(jobs.c.import_batch_id == filters["import_batch_id"])
        if filters.get("assigned") is not None:
            current = (
                select(project_plan_assignments.c.job_id)
                .join(
                    project_plan_versions,
                    project_plan_versions.c.id
                    == project_plan_assignments.c.plan_version_id,
                )
                .where(project_plan_versions.c.is_current.is_(True))
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
        self, project_id: int, values: dict[str, Any], actor_user_id: Any
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
        await self._session.execute(
            insert(job_planning_state).values(
                job_id=row.id,
                project_id=project_id,
                state="UNASSIGNED",
            )
        )
        event_id = await self._session.scalar(
            insert(planning_events)
            .values(
                project_id=project_id,
                event_type="JOB_CREATED",
                job_ids=[int(row.id)],
                    initiator="USER",
                actor_user_id=actor_user_id,
                idempotency_key=f"job-created:{row.id}",
                state="PENDING",
            )
            .returning(planning_events.c.id)
        )
        return {
            **(await self._result(row)),
            "planning_event_id": int(event_id),
            "planning_event_status_url": f"/api/project/planning/events/{event_id}",
        }

    async def import_jobs(
        self,
        project_id: int,
        rows: list[dict[str, Any]],
        actor_user_id: Any,
    ) -> dict[str, Any]:
        catalog = (
            (
                await self._session.execute(
                    select(work_types).where(
                        work_types.c.project_id == project_id,
                        work_types.c.active.is_(True),
                    )
                )
            )
            .mappings()
            .all()
        )
        by_key = {
            str(value).strip().casefold(): item
            for item in catalog
            for value in (item.code, item.name)
        }
        valid: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        received_at = datetime.now(timezone.utc)
        for number, values in enumerate(rows, start=2):
            work_type = by_key.get(
                str(values.get("work_type") or "").strip().casefold()
            )
            if work_type is None:
                errors.append({"row": number, "error": "WORK_TYPE_NOT_FOUND"})
                continue
            if not work_type.default_service_duration_min:
                errors.append({"row": number, "error": "MISSING_SERVICE_DURATION"})
                continue
            valid.append(
                {
                    "project_id": project_id,
                    "external_id": values.get("external_id") or None,
                    "internal_code": _code("JOB"),
                    "status": "NEW",
                    "received_at": received_at,
                    "ingest_sequence": number - 1,
                    "address": values["address"],
                    "latitude": values.get("latitude"),
                    "longitude": values.get("longitude"),
                    "sla_date": values["sla_date"],
                    "time_window_start": values.get("time_window_start"),
                    "time_window_end": values.get("time_window_end"),
                    "work_type_id": int(work_type.id),
                    "service_duration_min": int(work_type.default_service_duration_min),
                }
            )
        if errors:
            return {
                "created_job_ids": [],
                "created_count": 0,
                "errors": errors,
                "planning_event_id": None,
            }
        try:
            inserted_ids: list[int] = []
            for values in valid:
                inserted_id = int(
                    await self._session.scalar(
                        insert(jobs).values(**values).returning(jobs.c.id)
                    )
                )
                inserted_ids.append(inserted_id)
                await self._session.execute(
                    insert(job_planning_state).values(
                        job_id=inserted_id,
                        project_id=project_id,
                        state="UNASSIGNED",
                    )
                )
            event_id = None
            if inserted_ids:
                event_id = int(
                    await self._session.scalar(
                        insert(planning_events)
                        .values(
                            project_id=project_id,
                            event_type="IMPORT",
                            job_ids=inserted_ids,
                            initiator="USER",
                            actor_user_id=actor_user_id,
                            idempotency_key=f"import:{uuid.uuid4().hex}",
                            state="PENDING",
                        )
                        .returning(planning_events.c.id)
                    )
                )
        except IntegrityError as error:
            await self._session.rollback()
            raise InvalidPlanningRequest(
                "Import contains duplicate external_id values",
                code="DUPLICATE_EXTERNAL_ID",
            ) from error
        return {
            "created_job_ids": inserted_ids,
            "created_count": len(inserted_ids),
            "errors": errors,
            "planning_event_id": event_id,
        }

    async def get_job(self, project_id: int, job_id: int) -> dict[str, Any]:
        return await self._result(
            await _belongs(self._session, jobs, job_id, project_id)
        )

    async def load_job_for_update(
        self, project_id: int, job_id: int
    ) -> ProjectJobEditState:
        current = (
            (
                await self._session.execute(
                    select(jobs)
                    .where(jobs.c.id == job_id, jobs.c.project_id == project_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise ObjectNotFoundError("Object not found")
        published = await self._session.scalar(
            select(project_plan_assignments.c.id)
            .join(
                project_plan_versions,
                project_plan_versions.c.id
                == project_plan_assignments.c.plan_version_id,
            )
            .where(
                project_plan_assignments.c.job_id == job_id,
                project_plan_versions.c.is_current.is_(True),
            )
        )
        return ProjectJobEditState(
            id=int(current.id),
            project_id=int(current.project_id),
            status=current.status,
            published=published is not None,
            work_type_id=int(current.work_type_id),
            service_duration_min=current.service_duration_min,
            address=current.address,
            latitude=current.latitude,
            longitude=current.longitude,
            time_window_start=current.time_window_start,
            time_window_end=current.time_window_end,
        )

    async def get_work_type_for_edit(
        self, work_type_id: int
    ) -> WorkTypeEditState | None:
        row = (
            (
                await self._session.execute(
                    select(work_types).where(work_types.c.id == work_type_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return WorkTypeEditState(
            id=int(row.id),
            project_id=int(row.project_id),
            active=bool(row.active),
            default_service_duration_min=row.default_service_duration_min,
        )

    async def save_job(self, job_id: int, values: dict[str, Any]) -> dict[str, Any]:
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
        return await self._result(row)

    async def cancel_job(
        self, project_id: int, job_id: int, actor_user_id: Any
    ) -> dict[str, Any]:
        current = (
            (
                await self._session.execute(
                    select(jobs)
                    .where(jobs.c.id == job_id, jobs.c.project_id == project_id)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise ObjectNotFoundError("Job not found")
        if current.status == "COMPLETED":
            raise ConflictError(
                "Completed job cannot be cancelled", code="JOB_ALREADY_COMPLETED"
            )
        existing_event = (
            (
                await self._session.execute(
                    select(planning_events).where(
                        planning_events.c.project_id == project_id,
                        planning_events.c.idempotency_key == f"job-cancelled:{job_id}",
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if current.status == "CANCELLED":
            return {
                **(await self._result(current)),
                "planning_event_id": int(existing_event.id) if existing_event else None,
                "planning_event_state": existing_event.state
                if existing_event
                else None,
                "idempotent": True,
            }
        if current.status not in {"NEW", "IN_PROGRESS"}:
            raise ConflictError(
                "Only NEW or IN_PROGRESS job can be cancelled",
                code="INVALID_STATUS_TRANSITION",
            )
        now = datetime.now(timezone.utc)
        row = (
            (
                await self._session.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(
                        status="CANCELLED",
                        previous_status=current.status,
                        cancelled_at=now,
                        cancelled_by_user_id=actor_user_id,
                        updated_at=now,
                    )
                    .returning(*jobs.c)
                )
            )
            .mappings()
            .one()
        )
        current_assignment = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments.c.id,
                        project_plan_assignments.c.plan_version_id,
                    )
                    .join(
                        project_plan_versions,
                        project_plan_versions.c.id
                        == project_plan_assignments.c.plan_version_id,
                    )
                    .where(
                        project_plan_assignments.c.job_id == job_id,
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        assignment_id = current_assignment.id if current_assignment else None
        await self._session.execute(
            insert(job_status_history).values(
                job_id=job_id,
                project_plan_assignment_id=assignment_id,
                old_status=current.status,
                new_status="CANCELLED",
                actor_user_id=actor_user_id,
                reason="JOB_CANCELLED",
            )
        )
        event_id = int(
            await self._session.scalar(
                insert(planning_events)
                .values(
                    project_id=project_id,
                    event_type="JOB_CANCELLED",
                    job_ids=[job_id],
                    initiator="USER",
                    actor_user_id=actor_user_id,
                    idempotency_key=f"job-cancelled:{job_id}",
                    state="PENDING",
                    event_payload={
                        "previous_status": str(current.status),
                        "source_plan_version_id": (
                            int(current_assignment.plan_version_id)
                            if current_assignment
                            else None
                        ),
                        "cancelled_at": now.isoformat(),
                    },
                )
                .returning(planning_events.c.id)
            )
        )
        await self._session.execute(
            delete(job_planning_state).where(job_planning_state.c.job_id == job_id)
        )
        return {
            **(await self._result(row)),
            "planning_event_id": event_id,
            "planning_event_state": "PENDING",
            "planning_event_status_url": f"/api/project/planning/events/{event_id}",
            "idempotent": False,
        }

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
        priority = row.get("priority") if hasattr(row, "get") else None
        if priority is None:
            priority = await self._session.scalar(
                select(work_types.c.priority).where(
                    work_types.c.id == row.work_type_id
                )
            )
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
                        project_plan_assignments.c.id,
                        project_plan_assignments.c.engineer_id,
                        project_plan_assignments.c.sequence,
                        project_plan_assignments.c.planning_date,
                        project_plan_assignments.c.planned_start,
                        project_plan_assignments.c.planned_finish,
                        project_plan_versions.c.version_number.label("plan_version"),
                    )
                    .join(
                        project_plan_versions,
                        project_plan_versions.c.id
                        == project_plan_assignments.c.plan_version_id,
                    )
                    .where(
                        project_plan_assignments.c.job_id == row.id,
                        project_plan_versions.c.is_current.is_(True),
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
            "priority": priority or "LOW",
            "assignment": dict(assignment) if assignment else None,
            "status_history": [_dict(item, hidden=()) for item in history],
        }
