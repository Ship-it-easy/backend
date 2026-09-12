from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth.domain.entities.user import User
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineers,
    job_status_history,
    jobs,
    plan_versions,
)


async def current_assignment(session: AsyncSession, job_id: int, *, for_update: bool = False) -> Any:
    query = (
        select(assignments)
        .join(plan_versions, assignments.c.plan_version_id == plan_versions.c.id)
        .join(daily_plans, daily_plans.c.current_version_id == plan_versions.c.id)
        .where(assignments.c.job_id == job_id, assignments.c.active.is_(True))
    )
    if for_update:
        query = query.with_for_update()
    return (await session.execute(query)).mappings().one_or_none()


async def change_job_status(
    session: AsyncSession,
    user: User,
    job_id: int,
    new_status: str,
    *,
    project_id: int,
    engineer_id: int | None = None,
    reason: str | None = None,
    dispatcher: bool = False,
) -> dict[str, Any]:
    # Publication and execution take locks in the same daily-plan -> assignment
    # -> job order. Re-read after acquiring the plan lock in case publication
    # replaced the current version while this request was waiting.
    assignment = await current_assignment(session, job_id)
    if assignment is not None:
        daily_plan_id = await session.scalar(
            select(plan_versions.c.daily_plan_id).where(
                plan_versions.c.id == assignment.plan_version_id
            )
        )
        await session.scalar(
            select(daily_plans.c.id)
            .where(daily_plans.c.id == daily_plan_id)
            .with_for_update()
        )
        assignment = await current_assignment(session, job_id)
        if assignment is not None:
            await session.scalar(
                select(assignments.c.id)
                .where(assignments.c.id == assignment.id)
                .with_for_update()
            )
    job = (await session.execute(select(jobs).where(jobs.c.id == job_id, jobs.c.project_id == project_id).with_for_update())).mappings().one_or_none()
    if job is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Job not found"})
    if assignment is not None and engineer_id is not None and assignment.engineer_id != engineer_id:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "Assignment not found"})
    allowed = {
        "NEW": {"IN_PROGRESS", "CANCELLED"},
        "IN_PROGRESS": {"COMPLETED", "NEW", "CANCELLED"},
        "COMPLETED": {"IN_PROGRESS"},
        "CANCELLED": {"NEW"},
    }
    if new_status not in allowed.get(job.status, set()):
        _invalid("Requested status transition is not allowed")
    if not dispatcher and (job.status, new_status) not in {("NEW", "IN_PROGRESS"), ("IN_PROGRESS", "COMPLETED"), ("IN_PROGRESS", "NEW")}:
        _invalid("Engineer cannot perform this transition")
    if new_status in {"IN_PROGRESS", "COMPLETED"} and assignment is None:
        _invalid("Job has no current published assignment")
    if job.status == "COMPLETED" and new_status == "IN_PROGRESS" and not (reason and reason.strip()):
        _invalid("Reason is required to return a completed job")
    if new_status == "IN_PROGRESS":
        # Serialize starts for one engineer so two parallel requests cannot
        # create two IN_PROGRESS jobs.
        await session.scalar(
            select(engineers.c.id)
            .where(engineers.c.id == assignment.engineer_id)
            .with_for_update()
        )
        another = await session.scalar(
            select(jobs.c.id)
            .join(assignments, assignments.c.job_id == jobs.c.id)
            .where(assignments.c.engineer_id == assignment.engineer_id, assignments.c.active.is_(True), jobs.c.status == "IN_PROGRESS", jobs.c.id != job_id)
            .limit(1)
        )
        if another is not None:
            _invalid("Engineer already has a job in progress")
        previous_unfinished = await session.scalar(
            select(jobs.c.id)
            .join(assignments, assignments.c.job_id == jobs.c.id)
            .where(assignments.c.plan_version_id == assignment.plan_version_id, assignments.c.engineer_id == assignment.engineer_id, assignments.c.active.is_(True), assignments.c.sequence < assignment.sequence, jobs.c.status.not_in(["COMPLETED", "CANCELLED"]))
            .limit(1)
        )
        if previous_unfinished is not None:
            _invalid("Previous route jobs must be completed or cancelled first")
    if job.status in {"IN_PROGRESS", "COMPLETED"} and new_status in {"NEW", "IN_PROGRESS"}:
        later_started = await session.scalar(
            select(jobs.c.id)
            .join(assignments, assignments.c.job_id == jobs.c.id)
            .where(assignments.c.plan_version_id == assignment.plan_version_id, assignments.c.engineer_id == assignment.engineer_id, assignments.c.active.is_(True), assignments.c.sequence > assignment.sequence, jobs.c.status.in_(["IN_PROGRESS", "COMPLETED"]))
            .limit(1)
        )
        if later_started is not None:
            _invalid("A later route job has already started")
    await session.execute(
        update(jobs)
        .where(jobs.c.id == job_id)
        .values(status=new_status, updated_at=datetime.now(timezone.utc))
    )
    # CANCELLED -> NEW participates only in a future calculation, not in this published route.
    if job.status == "CANCELLED" and new_status == "NEW" and assignment is not None:
        await session.execute(update(assignments).where(assignments.c.id == assignment.id).values(active=False))
    await session.execute(insert(job_status_history).values(job_id=job_id, assignment_id=assignment.id if assignment else None, old_status=job.status, new_status=new_status, actor_user_id=user.id, reason=reason.strip() if reason else None))
    await session.commit()
    return {"job_id": job_id, "old_status": job.status, "status": new_status}


def _invalid(message: str) -> None:
    raise HTTPException(409, detail={"code": "INVALID_STATUS_TRANSITION", "message": message})
