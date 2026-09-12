from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.access import ProjectAccess
from planning.application.job_status import change_job_status
from planning.infrastructure.persistence.tables import (
    assignments,
    daily_plans,
    engineers,
    jobs,
    plan_versions,
    projects,
    work_types,
)

engineer_router = APIRouter(prefix="/api/engineer", tags=["Engineer"])


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
        .join(plan_versions, assignments.c.plan_version_id == plan_versions.c.id)
        .join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id)
        .join(jobs, assignments.c.job_id == jobs.c.id)
        .join(work_types, jobs.c.work_type_id == work_types.c.id)
        .where(assignments.c.engineer_id == engineer_id, assignments.c.active.is_(True))
    )


def _result(row: Any) -> dict[str, Any]:
    return dict(row)


@engineer_router.get("/assignments")
@inject
async def assignment_list(
    access: FromDishka[ProjectAccess],
    session: FromDishka[AsyncSession],
    scope: Literal["today", "future", "history"] = Query("today"),
) -> list[dict[str, Any]]:
    _, project_id, engineer_id = await access.engineer()
    timezone_name = await session.scalar(select(projects.c.planning_timezone).where(projects.c.id == project_id))
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
    query = _assignment_query(engineer_id)
    if scope == "today":
        query = query.where(
            or_(daily_plans.c.planning_date == today, jobs.c.status == "IN_PROGRESS"),
            jobs.c.status.not_in(["COMPLETED", "CANCELLED"]),
        )
    elif scope == "future":
        query = query.where(daily_plans.c.planning_date > today, jobs.c.status.not_in(["COMPLETED", "CANCELLED"]))
    else:
        query = query.where(jobs.c.status.in_(["COMPLETED", "CANCELLED"]))
    rows = (await session.execute(query.order_by(daily_plans.c.planning_date, assignments.c.sequence))).mappings().all()
    return [_result(row) for row in rows]


@engineer_router.get("/assignments/{assignment_id}")
@inject
async def assignment_get(assignment_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    _, _, engineer_id = await access.engineer()
    row = (await session.execute(_assignment_query(engineer_id).where(assignments.c.id == assignment_id))).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Assignment not found"})
    return _result(row)


async def _change(assignment_id: int, target: str, access: ProjectAccess, session: AsyncSession) -> dict[str, Any]:
    user, project_id, engineer_id = await access.engineer()
    assignment = (await session.execute(select(assignments, daily_plans.c.planning_date).join(plan_versions, assignments.c.plan_version_id == plan_versions.c.id).join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id).where(assignments.c.id == assignment_id, assignments.c.engineer_id == engineer_id, assignments.c.active.is_(True)))).mappings().one_or_none()
    if assignment is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Assignment not found"})
    timezone_name = await session.scalar(select(projects.c.planning_timezone).where(projects.c.id == project_id))
    today = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
    if target == "IN_PROGRESS" and assignment.planning_date != today:
        raise HTTPException(409, detail={"code": "INVALID_STATUS_TRANSITION", "message": "Only today's assignments can be changed by an engineer"})
    if target == "IN_PROGRESS" and not await session.scalar(select(engineers.c.active).where(engineers.c.id == engineer_id)):
        raise HTTPException(409, detail={"code": "INVALID_STATUS_TRANSITION", "message": "Inactive engineer cannot start a new job"})
    return await change_job_status(session, user, assignment.job_id, target, project_id=project_id, engineer_id=engineer_id)


@engineer_router.post("/assignments/{assignment_id}/start")
@inject
async def assignment_start(assignment_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    return await _change(assignment_id, "IN_PROGRESS", access, session)


@engineer_router.post("/assignments/{assignment_id}/complete")
@inject
async def assignment_complete(assignment_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    return await _change(assignment_id, "COMPLETED", access, session)


@engineer_router.post("/assignments/{assignment_id}/return-to-new")
@inject
async def assignment_return(assignment_id: int, access: FromDishka[ProjectAccess], session: FromDishka[AsyncSession]) -> dict[str, Any]:
    return await _change(assignment_id, "NEW", access, session)
