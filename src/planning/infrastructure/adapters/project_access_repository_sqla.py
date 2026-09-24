from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.interfaces.project_access_repository import (
    ProjectAccessRepository,
)
from planning.infrastructure.persistence_sqla.mappings.tables import (
    dispatcher_projects,
    projects,
)


class SqlaProjectAccessRepository(ProjectAccessRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_project_status(self, project_id: int) -> str | None:
        return await self._session.scalar(
            select(projects.c.status).where(projects.c.id == project_id)
        )

    async def dispatcher_has_project(self, user_id: UUID, project_id: int) -> bool:
        return (
            await self._session.scalar(
                select(dispatcher_projects.c.user_id).where(
                    dispatcher_projects.c.user_id == user_id,
                    dispatcher_projects.c.project_id == project_id,
                )
            )
            is not None
        )

    async def list_dispatcher_projects(
        self, user_id: UUID, *, active_only: bool
    ) -> list[dict[str, Any]]:
        query = (
            select(
                projects.c.id,
                projects.c.name,
                projects.c.planning_timezone,
                projects.c.status,
            )
            .join(
                dispatcher_projects,
                dispatcher_projects.c.project_id == projects.c.id,
            )
            .where(dispatcher_projects.c.user_id == user_id)
        )
        if active_only:
            query = query.where(projects.c.status == "ACTIVE")
        rows = (
            (await self._session.execute(query.order_by(projects.c.name)))
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]
