from datetime import datetime, timezone
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
    plan_versions,
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

    async def get_assignment(
        self,
        engineer_id: int,
        assignment_id: int,
    ) -> dict[str, Any]:
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
