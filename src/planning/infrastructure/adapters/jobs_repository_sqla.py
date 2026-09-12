from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.errors import InvalidPlanningRequest, ProjectNotFound
from planning.infrastructure.persistence_sqla.mappings.tables import (
    jobs,
    projects,
    work_types,
)


class SqlaJobsRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create_job(
        self, project_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        project = await self._session.scalar(
            select(projects.c.id).where(projects.c.id == project_id)
        )
        if project is None:
            raise ProjectNotFound("Project not found")
        work_type = await self._session.scalar(
            select(work_types.c.id).where(
                work_types.c.id == values["work_type_id"],
                work_types.c.project_id == project_id,
            )
        )
        if work_type is None:
            raise InvalidPlanningRequest("work_type_id does not belong to project")
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
            await self._session.commit()
        except IntegrityError as error:
            await self._session.rollback()
            raise InvalidPlanningRequest(
                "external_id already exists in project"
            ) from error
        return dict(row)

    async def list_jobs(self, project_id: int) -> list[dict[str, Any]]:
        exists = await self._session.scalar(
            select(projects.c.id).where(projects.c.id == project_id)
        )
        if exists is None:
            raise ProjectNotFound("Project not found")
        rows = (
            (
                await self._session.execute(
                    select(jobs)
                    .where(jobs.c.project_id == project_id)
                    .order_by(jobs.c.id)
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
