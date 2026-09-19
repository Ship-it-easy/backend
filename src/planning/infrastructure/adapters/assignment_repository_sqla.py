from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import ObjectNotFoundError
from planning.application.interfaces.assignment_repository import AssignmentRepository
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineers,
    jobs,
    plan_changes,
    plan_versions,
    project_plan_assignments,
    project_plan_versions,
    projects,
    work_types,
)


class SqlaAssignmentRepository(AssignmentRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_assignments(
        self,
        engineer_id: int,
        project_id: int,
        scope: str,
    ) -> list[dict[str, Any]]:
        timezone_name = await self._session.scalar(
            select(projects.c.planning_timezone).where(projects.c.id == project_id)
        )
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        dynamic_version = await self._session.scalar(
            select(project_plan_versions.c.id).where(
                project_plan_versions.c.project_id == project_id,
                project_plan_versions.c.is_current.is_(True),
            )
        )
        if dynamic_version is not None:
            query = self._project_assignment_query(engineer_id, dynamic_version)
            if scope == "today":
                query = query.where(
                    or_(
                        project_plan_assignments.c.planning_date == today,
                        jobs.c.status == "IN_PROGRESS",
                    ),
                    jobs.c.status.not_in(["COMPLETED", "CANCELLED"]),
                )
            elif scope == "future":
                query = query.where(
                    project_plan_assignments.c.planning_date > today,
                    jobs.c.status.not_in(["COMPLETED", "CANCELLED"]),
                )
            else:
                query = query.where(jobs.c.status.in_(["COMPLETED", "CANCELLED"]))
            rows = (
                (
                    await self._session.execute(
                        query.order_by(
                            project_plan_assignments.c.planning_date,
                            project_plan_assignments.c.sequence,
                        )
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]
        query = self._assignment_query(engineer_id)
        if scope == "today":
            query = query.where(
                or_(
                    daily_plans.c.planning_date == today, jobs.c.status == "IN_PROGRESS"
                ),
                jobs.c.status.not_in(["COMPLETED", "CANCELLED"]),
            )
        elif scope == "future":
            query = query.where(
                daily_plans.c.planning_date > today,
                jobs.c.status.not_in(["COMPLETED", "CANCELLED"]),
            )
        else:
            query = query.where(jobs.c.status.in_(["COMPLETED", "CANCELLED"]))
        rows = (
            (
                await self._session.execute(
                    query.order_by(daily_plans.c.planning_date, assignments.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def get_route_state(
        self,
        engineer_id: int,
        project_id: int,
        scope: str,
    ) -> dict[str, Any]:
        timezone_name = await self._session.scalar(
            select(projects.c.planning_timezone).where(projects.c.id == project_id)
        )
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        version = (
            (
                await self._session.execute(
                    select(
                        project_plan_versions.c.id,
                        project_plan_versions.c.version_number,
                        project_plan_versions.c.published_at,
                    ).where(
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        items = await self.list_assignments(engineer_id, project_id, scope)
        if version is None:
            return {
                "plan_version": None,
                "published_at": None,
                "assignments": items,
                "changes": [],
            }

        rows = (
            (
                await self._session.execute(
                    select(
                        plan_changes,
                        jobs.c.address,
                        work_types.c.name.label("work_type_name"),
                    )
                    .join(jobs, jobs.c.id == plan_changes.c.job_id)
                    .join(work_types, work_types.c.id == jobs.c.work_type_id)
                    .where(plan_changes.c.plan_version_id == version.id)
                    .order_by(plan_changes.c.id)
                )
            )
            .mappings()
            .all()
        )
        changes = []
        for row in rows:
            old_assignment = _engineer_assignment(row.old_assignment, engineer_id)
            new_assignment = _engineer_assignment(row.new_assignment, engineer_id)
            if old_assignment is None and new_assignment is None:
                continue
            if not _change_matches_scope(old_assignment, new_assignment, scope, today):
                continue
            if old_assignment is None:
                engineer_change_type = "ADDED"
            elif new_assignment is None:
                engineer_change_type = "REMOVED"
            else:
                engineer_change_type = "CHANGED"
            changes.append(
                {
                    "id": int(row.id),
                    "job_id": int(row.job_id),
                    "address": row.address,
                    "work_type_name": row.work_type_name,
                    "change_type": engineer_change_type,
                    "reason": row.reason,
                    # Never expose another engineer's assignment through this API.
                    "old_assignment": old_assignment,
                    "new_assignment": new_assignment,
                }
            )
        return {
            "plan_version": int(version.version_number),
            "published_at": version.published_at,
            "assignments": items,
            "changes": changes,
        }

    async def get_assignment(
        self,
        engineer_id: int,
        assignment_id: int,
    ) -> dict[str, Any]:
        dynamic_version = await self._session.scalar(
            select(project_plan_versions.c.id)
            .join(
                project_plan_assignments,
                project_plan_assignments.c.plan_version_id
                == project_plan_versions.c.id,
            )
            .where(
                project_plan_versions.c.is_current.is_(True),
                project_plan_assignments.c.id == assignment_id,
                project_plan_assignments.c.engineer_id == engineer_id,
            )
        )
        if dynamic_version is not None:
            return dict(
                (
                    await self._session.execute(
                        self._project_assignment_query(
                            engineer_id, dynamic_version
                        ).where(project_plan_assignments.c.id == assignment_id)
                    )
                )
                .mappings()
                .one()
            )
        row = (
            (
                await self._session.execute(
                    self._assignment_query(engineer_id).where(
                        assignments.c.id == assignment_id
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ObjectNotFoundError("Assignment not found")
        return dict(row)

    async def get_change_context(
        self,
        engineer_id: int,
        project_id: int,
        assignment_id: int,
    ) -> dict[str, Any]:
        row = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments.c.job_id,
                        project_plan_assignments.c.planning_date,
                    )
                    .join(
                        project_plan_versions,
                        project_plan_versions.c.id
                        == project_plan_assignments.c.plan_version_id,
                    )
                    .where(
                        project_plan_assignments.c.id == assignment_id,
                        project_plan_assignments.c.engineer_id == engineer_id,
                        project_plan_versions.c.project_id == project_id,
                        project_plan_versions.c.is_current.is_(True),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            row = (
                (
                    await self._session.execute(
                        select(assignments.c.job_id, daily_plans.c.planning_date)
                        .join(
                            plan_versions,
                            assignments.c.plan_version_id == plan_versions.c.id,
                        )
                        .join(
                            daily_plans,
                            daily_plans.c.current_version_id == plan_versions.c.id,
                        )
                        .where(
                            assignments.c.id == assignment_id,
                            assignments.c.engineer_id == engineer_id,
                            assignments.c.active.is_(True),
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ObjectNotFoundError("Assignment not found")
        timezone_name = await self._session.scalar(
            select(projects.c.planning_timezone).where(projects.c.id == project_id)
        )
        engineer_active = await self._session.scalar(
            select(engineers.c.active).where(engineers.c.id == engineer_id)
        )
        return {
            "job_id": int(row.job_id),
            "planning_date": row.planning_date,
            "today": datetime.now(timezone.utc)
            .astimezone(ZoneInfo(timezone_name))
            .date(),
            "engineer_active": bool(engineer_active),
        }

    @staticmethod
    def _assignment_query(engineer_id: int):
        return (
            select(
                assignments,
                daily_plans.c.planning_date,
                jobs.c.address,
                jobs.c.latitude,
                jobs.c.longitude,
                jobs.c.sla_date,
                jobs.c.time_window_start,
                jobs.c.time_window_end,
                jobs.c.service_duration_min,
                jobs.c.status.label("job_status"),
                jobs.c.priority_type,
                work_types.c.name.label("work_type_name"),
            )
            .join(
                plan_versions,
                assignments.c.plan_version_id == plan_versions.c.id,
            )
            .join(
                daily_plans,
                daily_plans.c.current_version_id == plan_versions.c.id,
            )
            .join(jobs, assignments.c.job_id == jobs.c.id)
            .join(work_types, jobs.c.work_type_id == work_types.c.id)
            .where(
                assignments.c.engineer_id == engineer_id,
                assignments.c.active.is_(True),
            )
        )

    @staticmethod
    def _project_assignment_query(engineer_id: int, version_id: int):
        return (
            select(
                project_plan_assignments,
                project_plan_versions.c.version_number.label("plan_version"),
                jobs.c.address,
                jobs.c.latitude,
                jobs.c.longitude,
                jobs.c.sla_date,
                jobs.c.time_window_start,
                jobs.c.time_window_end,
                jobs.c.service_duration_min,
                jobs.c.status.label("job_status"),
                jobs.c.priority_type,
                work_types.c.name.label("work_type_name"),
            )
            .join(
                project_plan_versions,
                project_plan_versions.c.id
                == project_plan_assignments.c.plan_version_id,
            )
            .join(jobs, project_plan_assignments.c.job_id == jobs.c.id)
            .join(work_types, jobs.c.work_type_id == work_types.c.id)
            .where(
                project_plan_assignments.c.plan_version_id == version_id,
                project_plan_assignments.c.engineer_id == engineer_id,
            )
        )


def _engineer_assignment(value: Any, engineer_id: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        belongs_to_engineer = int(value.get("engineer_id")) == engineer_id
    except (TypeError, ValueError):
        belongs_to_engineer = False
    return dict(value) if belongs_to_engineer else None


def _change_matches_scope(
    old_assignment: dict[str, Any] | None,
    new_assignment: dict[str, Any] | None,
    scope: str,
    today: date,
) -> bool:
    if scope == "history":
        return False
    dates = []
    for assignment in (old_assignment, new_assignment):
        if not assignment or not assignment.get("planning_date"):
            continue
        try:
            dates.append(date.fromisoformat(str(assignment["planning_date"])))
        except ValueError:
            continue
    if scope == "today":
        return today in dates
    return any(value > today for value in dates)
