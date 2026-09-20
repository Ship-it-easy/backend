import logging
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from planning.application.access import ProjectAccess
from planning.application.errors import ConflictError, ObjectNotFoundError
from planning.application.interfaces.dynamic_planning_repository import (
    DynamicPlanningRepository,
)
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
    PlanningBatchRepository,
)
from planning.application.interfaces.project_management_repositories import (
    PlanningManagementRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager

logger = logging.getLogger(__name__)


class StartDynamicPlanningInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: DynamicPlanningRepository,
        executor: PlanningBatchExecutor,
        batch_repository: PlanningBatchRepository,
        management_repository: PlanningManagementRepository,
        transaction_manager: TransactionManager,
    ):
        self._access = access
        self._repository = repository
        self._executor = executor
        self._batch_repository = batch_repository
        self._management_repository = management_repository
        self._transaction_manager = transaction_manager

    async def __call__(
        self, idempotency_key: str, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            user, project_id = await self._access.dispatcher()
            status_url = "/api/project/planning/events/{event_id}"
        else:
            project_id = scoped_project_id
            user = await self._access.project(project_id, write=True)
            status_url = f"/api/projects/{project_id}/planning/events/{{event_id}}"
        active = await self._repository.get_active_event(project_id)
        if active is not None:
            return {
                "planning_event_id": active["id"],
                "state": active["state"],
                "status_url": status_url.format(event_id=active["id"]),
                "reuse": True,
            }
        timezone_name = await self._batch_repository.get_project_timezone(project_id)
        project_date = (
            datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        )
        readiness = await self._management_repository.get_readiness_state(
            project_id, project_date
        )
        if not all(
            (
                readiness.has_config,
                readiness.has_work_types,
                readiness.has_engineers,
                readiness.has_shifts,
                readiness.has_jobs,
                readiness.has_engineer_locations,
                readiness.has_job_locations,
                readiness.has_travel_provider,
            )
        ):
            raise ConflictError(
                "Planning source data is not ready",
                code="PLANNING_DATA_NOT_READY",
            )
        event = await self._repository.enqueue(
            project_id, "MANUAL", user.id, idempotency_key
        )
        await self._transaction_manager.commit()
        self._executor.schedule_project(project_id)
        logger.info(
            "planning_run_started project_id=%s event_id=%s actor_user_id=%s",
            project_id,
            event["id"],
            user.id,
        )
        return {
            "planning_event_id": event["id"],
            "state": event["state"],
            "status_url": status_url.format(event_id=event["id"]),
            "reuse": bool(event.get("_reused_active")),
        }


class GetPlanningEventInteractor:
    def __init__(self, access: ProjectAccess, repository: DynamicPlanningRepository):
        self._access = access
        self._repository = repository

    async def __call__(
        self, event_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        event = await self._repository.get_event(project_id, event_id)
        if event is None:
            raise ObjectNotFoundError("Planning event not found")
        return event


class GetCurrentProjectPlanInteractor:
    def __init__(self, access: ProjectAccess, repository: DynamicPlanningRepository):
        self._access = access
        self._repository = repository

    async def __call__(self, scoped_project_id: int | None = None) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        plan = await self._repository.get_current_plan(project_id)
        return plan or {"version": None, "assignments": [], "changes": []}


class ListProjectPlanVersionsInteractor:
    def __init__(self, access: ProjectAccess, repository: DynamicPlanningRepository):
        self._access = access
        self._repository = repository

    async def __call__(
        self,
        *,
        limit: int,
        offset: int,
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        return await self._repository.list_plan_versions(project_id, limit, offset)


class GetProjectPlanVersionInteractor:
    def __init__(self, access: ProjectAccess, repository: DynamicPlanningRepository):
        self._access = access
        self._repository = repository

    async def __call__(
        self, version_id: int, scoped_project_id: int | None = None
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        plan = await self._repository.get_plan_version(project_id, version_id)
        if plan is None:
            raise ObjectNotFoundError("Plan version not found")
        return plan


class GetPlanningBoardInteractor:
    def __init__(
        self,
        access: ProjectAccess,
        repository: DynamicPlanningRepository,
        batch_repository: PlanningBatchRepository,
        management_repository: PlanningManagementRepository,
    ):
        self._access = access
        self._repository = repository
        self._batch_repository = batch_repository
        self._management_repository = management_repository

    async def __call__(
        self,
        *,
        from_date: date | None,
        days: int,
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        timezone_name = await self._batch_repository.get_project_timezone(project_id)
        project_date = (
            datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
        )
        requested_from = from_date or project_date
        # The product screen intentionally exposes only D0..D0+6.
        if requested_from != project_date or days != 7:
            raise ConflictError(
                "Planning board supports only the current seven-day range",
                code="PLANNING_BOARD_RANGE_INVALID",
            )
        result = await self._repository.get_planning_board_summary(
            project_id, project_date, 7
        )
        readiness = await self._management_repository.get_readiness_state(
            project_id, project_date
        )
        checks = (
            (
                "PLANNING_CONFIG_MISSING",
                "Не настроены параметры планирования",
                readiness.has_config,
                "parameters",
            ),
            (
                "WORK_TYPES_MISSING",
                "Нет активных типов работ",
                readiness.has_work_types,
                "catalogs",
            ),
            (
                "ENGINEERS_MISSING",
                "Нет активных инженеров",
                readiness.has_engineers,
                "engineers",
            ),
            (
                "SHIFTS_MISSING",
                "Нет инженеров со сменой на текущую дату",
                readiness.has_shifts,
                "engineers",
            ),
            (
                "JOBS_MISSING",
                "Нет новых заявок для расчёта",
                readiness.has_jobs,
                "jobs",
            ),
            (
                "ENGINEER_LOCATIONS_MISSING",
                "У инженера не указан стартовый адрес или координаты",
                readiness.has_engineer_locations,
                "engineers",
            ),
            (
                "JOB_LOCATIONS_MISSING",
                "У заявки не указан адрес или координаты",
                readiness.has_job_locations,
                "jobs",
            ),
            (
                "TRAVEL_PROVIDER_MISSING",
                "Не настроен поддерживаемый дорожный сервис",
                readiness.has_travel_provider,
                "parameters",
            ),
        )
        problems = [
            {"code": code, "message": message, "section": section}
            for code, message, available, section in checks
            if not available
        ]
        result["readiness"] = {"ready": not problems, "problems": problems}
        return result


class GetPlanningBoardDayInteractor:
    def __init__(self, access: ProjectAccess, repository: DynamicPlanningRepository):
        self._access = access
        self._repository = repository

    async def __call__(
        self,
        planning_date: date,
        plan_version_id: int | None,
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        result = await self._repository.get_planning_board_day(
            project_id, planning_date, plan_version_id
        )
        if result.get("version_changed"):
            logger.info(
                "planning_version_changed project_id=%s requested_version_id=%s "
                "current_version_id=%s",
                project_id,
                plan_version_id,
                result.get("current_plan_version_id"),
            )
            raise ConflictError(
                "The current plan version changed",
                code="VERSION_CHANGED",
            )
        logger.info(
            "planning_day_selected project_id=%s plan_version_id=%s planning_date=%s",
            project_id,
            result.get("plan_version_id"),
            planning_date,
        )
        return result


class GetPlanningJobExplanationInteractor:
    def __init__(self, access: ProjectAccess, repository: DynamicPlanningRepository):
        self._access = access
        self._repository = repository

    async def __call__(
        self,
        day_result_id: int,
        job_id: int,
        scoped_project_id: int | None = None,
    ) -> dict[str, Any]:
        if scoped_project_id is None:
            _, project_id = await self._access.dispatcher()
        else:
            project_id = scoped_project_id
            await self._access.project(project_id)
        result = await self._repository.get_planning_job_explanation(
            project_id, day_result_id, job_id
        )
        if result is None:
            raise ObjectNotFoundError("Planning job result not found")
        logger.info(
            "planning_job_details_opened project_id=%s day_result_id=%s job_id=%s",
            project_id,
            day_result_id,
            job_id,
        )
        return result
