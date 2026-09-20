from datetime import date
from typing import Any

from planning.application.access import ProjectAccess
from planning.application.errors import (
    ConflictError,
    InvalidJobStatusError,
    InvalidPlanningRequest,
)
from planning.application.interactors.job_status.change_job_status import (
    ChangeJobStatusInteractor,
)
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
)
from planning.application.interfaces.project_management_repositories import (
    ProjectJobsRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager


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
        import_batch_id: int | None = None,
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
                "import_batch_id": import_batch_id,
                "limit": limit,
                "offset": offset,
            },
        )


class CreateProjectJobInteractor(_ProjectJobsInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: ProjectJobsRepository,
        executor: PlanningBatchExecutor,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository)
        self._executor = executor
        self._transaction_manager = transaction_manager

    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        result = await self._repository.create_job(project_id, values, user.id)
        await self._transaction_manager.commit()
        self._executor.schedule_project(project_id)
        return result


class GetProjectJobInteractor(_ProjectJobsInteractor):
    async def __call__(self, job_id: int) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_job(project_id, job_id)


class UpdateProjectJobInteractor(_ProjectJobsInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: ProjectJobsRepository,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository)
        self._transaction_manager = transaction_manager

    async def __call__(self, job_id: int, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        if "priority_type" in values:
            raise ConflictError(
                "Job priority type is immutable",
                code="PRIORITY_TYPE_IMMUTABLE",
            )
        current = await self._repository.load_job_for_update(project_id, job_id)
        if current.status != "NEW":
            raise InvalidJobStatusError("Only NEW jobs may be edited")
        if current.published:
            raise ConflictError("Published job cannot be edited", code="JOB_PUBLISHED")

        work_type_id = int(values.get("work_type_id", current.work_type_id))
        work_type = await self._repository.get_work_type_for_edit(work_type_id)
        if (
            work_type is None
            or work_type.project_id != project_id
            or not work_type.active
        ):
            raise InvalidPlanningRequest(
                "Active work type not found in project",
                code="CROSS_PROJECT_REFERENCE",
            )
        if (
            work_type.default_service_duration_min is None
            or work_type.default_service_duration_min <= 0
        ):
            raise InvalidPlanningRequest(
                "Work type has no positive duration",
                code="MISSING_SERVICE_DURATION",
            )

        changes = dict(values)
        changes["service_duration_min"] = work_type.default_service_duration_min
        coordinate_keys = {"latitude", "longitude"}
        supplied_coordinates = coordinate_keys.intersection(changes)
        if supplied_coordinates and supplied_coordinates != coordinate_keys:
            raise InvalidPlanningRequest(
                "Coordinates must be provided together",
                code="INVALID_COORDINATES",
            )
        if "address" in changes and not supplied_coordinates:
            changes.update(
                latitude=None,
                longitude=None,
                address_hash=None,
                geocoded_at=None,
            )

        final_latitude = changes.get("latitude", current.latitude)
        final_longitude = changes.get("longitude", current.longitude)
        if (final_latitude is None) != (final_longitude is None):
            raise InvalidPlanningRequest(
                "Coordinates must be provided together",
                code="INVALID_COORDINATES",
            )
        final_start = changes.get("time_window_start", current.time_window_start)
        final_end = changes.get("time_window_end", current.time_window_end)
        if (
            final_start is not None
            and final_end is not None
            and final_start >= final_end
        ):
            raise InvalidPlanningRequest(
                "Time window cannot cross midnight",
                code="INVALID_TIME_WINDOW",
            )

        result = await self._repository.save_job(job_id, changes)
        await self._transaction_manager.commit()
        return result


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


class CancelProjectJobInteractor(_ProjectJobsInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: ProjectJobsRepository,
        executor: PlanningBatchExecutor,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository)
        self._executor = executor
        self._transaction_manager = transaction_manager

    async def __call__(
        self, job_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            user, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            user = await self._access.project(project_id, write=True)
        result = await self._repository.cancel_job(project_id, job_id, user.id)
        if (
            scoped_project_id is not None
            and result.get("planning_event_id") is not None
        ):
            result["planning_event_status_url"] = (
                f"/api/projects/{project_id}/planning/events/"
                f"{result['planning_event_id']}"
            )
        await self._transaction_manager.commit()
        if result.get("planning_event_state") == "PENDING":
            self._executor.schedule_project(project_id)
        return result
