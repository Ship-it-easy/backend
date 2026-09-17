from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import ObjectNotFoundError
from planning.application.interfaces.job_status_repository import (
    JobStatusContext,
    JobStatusRepository,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    assignments,
    daily_plans,
    engineers,
    job_status_history,
    jobs,
    plan_versions,
    project_plan_assignments,
    project_plan_versions,
)


class SqlaJobStatusRepository(JobStatusRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def load_transition_context(
        self,
        job_id: int,
        project_id: int,
        engineer_id: int | None,
        new_status: str,
    ) -> JobStatusContext:
        assignment = await self._current_assignment(job_id)
        if assignment is not None:
            if assignment.dynamic:
                await self._session.scalar(
                    select(project_plan_versions.c.id)
                    .where(project_plan_versions.c.id == assignment.plan_version_id)
                    .with_for_update()
                )
            else:
                daily_plan_id = await self._session.scalar(
                    select(plan_versions.c.daily_plan_id).where(
                        plan_versions.c.id == assignment.plan_version_id
                    )
                )
                await self._session.scalar(
                    select(daily_plans.c.id)
                    .where(daily_plans.c.id == daily_plan_id)
                    .with_for_update()
                )
            assignment = await self._current_assignment(job_id)
            if assignment is not None:
                table = project_plan_assignments if assignment.dynamic else assignments
                await self._session.scalar(
                    select(table.c.id)
                    .where(table.c.id == assignment.id)
                    .with_for_update()
                )

        job = (
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
        if job is None:
            raise ObjectNotFoundError("Job not found")

        assignment_matches_engineer = (
            assignment is None
            or engineer_id is None
            or assignment.engineer_id == engineer_id
        )
        another_job_in_progress = False
        previous_job_unfinished = False
        later_job_started = False

        if new_status == "IN_PROGRESS" and assignment is not None:
            await self._session.scalar(
                select(engineers.c.id)
                .where(engineers.c.id == assignment.engineer_id)
                .with_for_update()
            )
            assignment_table = (
                project_plan_assignments if assignment.dynamic else assignments
            )
            another_job_in_progress = (
                await self._session.scalar(
                    select(jobs.c.id)
                    .join(assignment_table, assignment_table.c.job_id == jobs.c.id)
                    .where(
                        assignment_table.c.engineer_id == assignment.engineer_id,
                        *(
                            [
                                assignment_table.c.plan_version_id
                                == assignment.plan_version_id
                            ]
                            if assignment.dynamic
                            else [assignment_table.c.active.is_(True)]
                        ),
                        jobs.c.status == "IN_PROGRESS",
                        jobs.c.id != job_id,
                    )
                    .limit(1)
                )
                is not None
            )
            previous_job_unfinished = (
                await self._session.scalar(
                    select(jobs.c.id)
                    .join(assignment_table, assignment_table.c.job_id == jobs.c.id)
                    .where(
                        assignment_table.c.plan_version_id
                        == assignment.plan_version_id,
                        assignment_table.c.engineer_id == assignment.engineer_id,
                        assignment_table.c.sequence < assignment.sequence,
                        *(
                            []
                            if assignment.dynamic
                            else [assignment_table.c.active.is_(True)]
                        ),
                        jobs.c.status.not_in(["COMPLETED", "CANCELLED"]),
                    )
                    .limit(1)
                )
                is not None
            )

        if (
            assignment is not None
            and job.status in {"IN_PROGRESS", "COMPLETED"}
            and new_status in {"NEW", "IN_PROGRESS"}
        ):
            assignment_table = (
                project_plan_assignments if assignment.dynamic else assignments
            )
            later_job_started = (
                await self._session.scalar(
                    select(jobs.c.id)
                    .join(assignment_table, assignment_table.c.job_id == jobs.c.id)
                    .where(
                        assignment_table.c.plan_version_id
                        == assignment.plan_version_id,
                        assignment_table.c.engineer_id == assignment.engineer_id,
                        assignment_table.c.sequence > assignment.sequence,
                        *(
                            []
                            if assignment.dynamic
                            else [assignment_table.c.active.is_(True)]
                        ),
                        jobs.c.status.in_(["IN_PROGRESS", "COMPLETED"]),
                    )
                    .limit(1)
                )
                is not None
            )

        return JobStatusContext(
            job_id=job_id,
            old_status=str(job.status),
            assignment_id=(
                int(assignment.id)
                if assignment is not None and not assignment.dynamic
                else None
            ),
            project_assignment_id=(
                int(assignment.id)
                if assignment is not None and assignment.dynamic
                else None
            ),
            assignment_engineer_id=(
                int(assignment.engineer_id) if assignment is not None else None
            ),
            assignment_matches_engineer=assignment_matches_engineer,
            another_job_in_progress=another_job_in_progress,
            previous_job_unfinished=previous_job_unfinished,
            later_job_started=later_job_started,
        )

    async def save_transition(
        self,
        context: JobStatusContext,
        new_status: str,
        actor_user_id: UUID,
        reason: str | None,
        *,
        deactivate_assignment: bool,
    ) -> dict[str, Any]:
        await self._session.execute(
            update(jobs)
            .where(jobs.c.id == context.job_id)
            .values(status=new_status, updated_at=datetime.now(timezone.utc))
        )
        if deactivate_assignment and context.assignment_id is not None:
            await self._session.execute(
                update(assignments)
                .where(assignments.c.id == context.assignment_id)
                .values(active=False)
            )
        await self._session.execute(
            insert(job_status_history).values(
                job_id=context.job_id,
                assignment_id=context.assignment_id,
                project_plan_assignment_id=context.project_assignment_id,
                old_status=context.old_status,
                new_status=new_status,
                actor_user_id=actor_user_id,
                reason=reason,
            )
        )
        return {
            "job_id": context.job_id,
            "old_status": context.old_status,
            "status": new_status,
        }

    async def _current_assignment(self, job_id: int):
        dynamic = (
            (
                await self._session.execute(
                    select(
                        project_plan_assignments,
                        project_plan_versions.c.is_current,
                    )
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
            )
            .mappings()
            .one_or_none()
        )
        if dynamic is not None:
            return SimpleNamespace(**dict(dynamic), dynamic=True)
        query = (
            select(assignments)
            .join(
                plan_versions,
                assignments.c.plan_version_id == plan_versions.c.id,
            )
            .join(
                daily_plans,
                daily_plans.c.current_version_id == plan_versions.c.id,
            )
            .where(
                assignments.c.job_id == job_id,
                assignments.c.active.is_(True),
            )
        )
        legacy = (await self._session.execute(query)).mappings().one_or_none()
        return (
            SimpleNamespace(**dict(legacy), dynamic=False)
            if legacy is not None
            else None
        )
