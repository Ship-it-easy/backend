from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from planning.application.access import ProjectAccess
from planning.application.errors import InvalidPlanningRequest
from planning.application.interfaces.dynamic_planning_repository import (
    DynamicPlanningRepository,
)
from planning.application.interfaces.planning_batch_repository import (
    PlanningBatchExecutor,
    PlanningBatchRepository,
)
from planning.application.interfaces.transaction_manager import TransactionManager


class StartPlanningRunInteractor:
    """Compatibility entry point for the project-wide manual planning event."""

    def __init__(
        self,
        access: ProjectAccess,
        repository: PlanningBatchRepository,
        dynamic_repository: DynamicPlanningRepository,
        executor: PlanningBatchExecutor,
        transaction_manager: TransactionManager,
    ):
        self._access = access
        self._repository = repository
        self._dynamic_repository = dynamic_repository
        self._executor = executor
        self._transaction_manager = transaction_manager

    async def __call__(
        self,
        project_id: int,
        planning_date: date,
        timezone_name: str | None,
        idempotency_key: str,
    ) -> dict:
        user = await self._access.project(project_id, write=True)
        project_timezone = await self._repository.get_project_timezone(project_id)
        if timezone_name is not None and timezone_name != project_timezone:
            raise InvalidPlanningRequest(
                "timezone must match the project planning timezone"
            )
        self._validate_date(planning_date, project_timezone)
        event = await self._dynamic_repository.enqueue(
            project_id,
            "MANUAL",
            user.id,
            f"manual-run:{idempotency_key}",
        )
        await self._transaction_manager.commit()
        self._executor.schedule_project(project_id)
        return {
            "planning_event_id": event["id"],
            "planning_run_id": None,
            "status": event["state"],
            "status_url": f"/api/projects/{project_id}/planning/events/{event['id']}",
        }

    @staticmethod
    def _validate_date(planning_date: date, timezone_name: str) -> None:
        try:
            current_date = (
                datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date()
            )
        except ZoneInfoNotFoundError as error:
            raise InvalidPlanningRequest("Unknown IANA timezone") from error
        if planning_date != current_date:
            raise InvalidPlanningRequest(
                "manual planning date must equal the current project date"
            )
