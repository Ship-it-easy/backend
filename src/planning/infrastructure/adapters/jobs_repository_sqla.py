import uuid
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import InvalidPlanningRequest, ProjectNotFound
from planning.infrastructure.persistence_sqla.mappings.tables import (
    job_planning_state,
    jobs,
    planning_events,
    projects,
    work_types,
)


class SqlaJobsRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_job(
        self, project_id: int, values: dict[str, Any], actor_user_id: Any
    ) -> dict[str, Any]:
        project = await self._session.scalar(
            select(projects.c.id).where(projects.c.id == project_id)
        )
        if project is None:
            raise ProjectNotFound("Project not found")
        work_type = (
            await self._session.execute(
                select(
                    work_types.c.id,
                    work_types.c.default_service_duration_min,
                    work_types.c.priority,
                ).where(
                    work_types.c.id == values["work_type_id"],
                    work_types.c.project_id == project_id,
                    work_types.c.active.is_(True),
                )
            )
        ).one_or_none()
        if work_type is None:
            raise InvalidPlanningRequest("work_type_id does not belong to project")
        values.setdefault("internal_code", f"JOB-{uuid.uuid4().hex[:12].upper()}")
        values["service_duration_min"] = work_type.default_service_duration_min
        if not values["service_duration_min"]:
            raise InvalidPlanningRequest("work type has no positive service duration")
        try:
            row = (
                (
                    await self._session.execute(
                        insert(jobs)
                        .values(project_id=project_id, status="NEW", **values)
                        .returning(*jobs.c)
                    )
                )
                .mappings()
                .one()
            )
            await self._session.execute(
                insert(job_planning_state).values(
                    job_id=row.id, project_id=project_id, state="UNASSIGNED"
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
        except IntegrityError as error:
            await self._session.rollback()
            raise InvalidPlanningRequest(
                "external_id already exists in project"
            ) from error
        return {
            **dict(row),
            "priority": work_type.priority,
            "planning_event_id": int(event_id),
        }

    async def list_jobs(
        self, project_id: int, import_batch_id: int | None = None
    ) -> list[dict[str, Any]]:
        exists = await self._session.scalar(
            select(projects.c.id).where(projects.c.id == project_id)
        )
        if exists is None:
            raise ProjectNotFound("Project not found")
        query = (
            select(jobs, work_types.c.priority.label("priority"))
            .join(work_types, work_types.c.id == jobs.c.work_type_id)
            .where(jobs.c.project_id == project_id)
        )
        if import_batch_id is not None:
            query = query.where(jobs.c.import_batch_id == import_batch_id)
        rows = (
            (
                await self._session.execute(
                    query.order_by(jobs.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
