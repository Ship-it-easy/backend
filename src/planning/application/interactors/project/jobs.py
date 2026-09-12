from datetime import date
from typing import Any

from planning.application.access import ProjectAccess
from planning.application.interactors.job_status.change_job_status import (
    ChangeJobStatusInteractor,
)
from planning.application.interfaces.project_management_repositories import (
    ProjectJobsRepository,
)


class _ProjectJobsInteractor:
    def __init__(self, access: ProjectAccess, repository: ProjectJobsRepository):
        self._access = access
        self._repository = repository


class ListProjectJobsInteractor(_ProjectJobsInteractor):
    async def __call__(
        self,
        *,
        search: str | None,
        status: str | None,
        sla_date: date | None,
        work_type_id: int | None,
        assigned: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.list_jobs(
            project_id,
            {
                "search": search,
                "status": status,
                "sla_date": sla_date,
                "work_type_id": work_type_id,
                "assigned": assigned,
                "limit": limit,
                "offset": offset,
            },
        )


class CreateProjectJobInteractor(_ProjectJobsInteractor):
    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.create_job(project_id, values)


class GetProjectJobInteractor(_ProjectJobsInteractor):
    async def __call__(self, job_id: int) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_job(project_id, job_id)


class UpdateProjectJobInteractor(_ProjectJobsInteractor):
    async def __call__(self, job_id: int, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.update_job(project_id, job_id, values)


class ChangeProjectJobStatusInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        change_status: ChangeJobStatusInteractor,
    ):
        self._access = access
        self._change_status = change_status

    async def __call__(
        self,
        job_id: int,
        status: str,
        reason: str | None,
    ) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        return await self._change_status(
            user,
            job_id,
            status,
            project_id=project_id,
            reason=reason,
            dispatcher=True,
        )
