from datetime import date
from typing import Any

from planning.application.access import ProjectAccess
from planning.application.errors import ConflictError, ObjectNotFoundError
from planning.application.interfaces.project_management_repositories import (
    PlanningManagementRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager
from planning.application.management_dto import PublishPlanCommand


class _PlanningManagementInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: PlanningManagementRepository,
    ):
        self._access = access
        self._repository = repository


class GetPlanningConfigInteractor(_PlanningManagementInteractor):
    async def __call__(self) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_config(project_id)


class UpdatePlanningConfigInteractor(_PlanningManagementInteractor):
    async def __call__(self, values: dict[str, Any]) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.update_config(project_id, values)


class CheckPlanningReadinessInteractor(_PlanningManagementInteractor):
    async def __call__(self, planning_date: date) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        state = await self._repository.get_readiness_state(project_id, planning_date)
        checks = (
            (
                "PLANNING_CONFIG_MISSING",
                "Active planning configuration is missing",
                state.has_config,
            ),
            (
                "WORK_TYPES_MISSING",
                "Create at least one active work type",
                state.has_work_types,
            ),
            (
                "ENGINEERS_MISSING",
                "No active engineer has a shift on this date",
                state.has_engineers,
            ),
            ("JOBS_MISSING", "No NEW jobs are available", state.has_jobs),
        )
        problems = [
            {"code": code, "message": message}
            for code, message, available in checks
            if not available
        ]
        return {
            "planning_date": planning_date,
            "ready": not problems,
            "problems": problems,
        }


class PublishPlanningRunInteractor(_PlanningManagementInteractor):
    def __init__(
        self,
        access: ProjectAccess,
        repository: PlanningManagementRepository,
        transaction_manager: TransactionManager,
    ):
        super().__init__(access, repository)
        self._transaction_manager = transaction_manager

    async def __call__(
        self,
        run_id: int,
        confirm_unassigned: bool,
    ) -> dict[str, Any]:
        user, project_id = await self._access.dispatcher()
        state = await self._repository.load_publication_state(project_id, run_id)
        if state is None:
            raise ObjectNotFoundError("Planning run not found")
        if state.status != "SUCCESS" or state.validation_errors:
            raise ConflictError(
                "Only a validated successful run can be published",
                code="PLANNING_RUN_NOT_PUBLISHABLE",
            )
        if state.unassigned_jobs_count and not confirm_unassigned:
            raise ConflictError(
                "Confirm publication with unassigned jobs",
                code="UNASSIGNED_CONFIRMATION_REQUIRED",
            )
        if state.already_published:
            raise ConflictError(
                "Planning run is already published", code="ALREADY_PUBLISHED"
            )
        if state.current_plan_started:
            raise ConflictError(
                "Published plan cannot be replaced after work started",
                code="PLAN_ALREADY_STARTED",
            )
        assigned_job_ids = {item.job_id for item in state.routes}
        if assigned_job_ids != set(state.publishable_job_ids):
            raise ConflictError(
                "Assigned jobs changed after the calculation",
                code="CONCURRENT_MODIFICATION",
            )

        command = PublishPlanCommand(
            project_id=project_id,
            run_id=run_id,
            planning_date=state.planning_date,
            published_by=user.id,
            daily_plan_id=state.daily_plan_id,
            previous_version_id=state.current_version_id,
            version_number=state.max_version_number + 1,
            assignments=state.routes,
            snapshots=state.snapshots,
        )
        published = await self._repository.save_publication(command)
        await self._transaction_manager.commit()
        return published.response()


class GetDailyPlanInteractor(_PlanningManagementInteractor):
    async def __call__(self, planning_date: date) -> dict[str, Any]:
        _, project_id = await self._access.dispatcher()
        return await self._repository.get_daily_plan(project_id, planning_date)
