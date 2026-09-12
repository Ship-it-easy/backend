from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from planning.application.interfaces.project_access_repository import (
    ProjectAccessRepository,
)
from planning.infrastructure.persistence_sqla.mappings.tables import projects


class SqlaProjectAccessRepository(ProjectAccessRepository):
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_project_status(self, project_id: int) -> str | None:
        return await self._session.scalar(
            select(projects.c.status).where(projects.c.id == project_id)
        )
