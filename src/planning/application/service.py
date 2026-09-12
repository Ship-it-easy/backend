import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from auth.application.interfaces.identity_provider import IdentityProvider
from auth.domain.errors import AccessControlError
from auth.domain.user_role import UserRoleEnum, has_required_role
from planning.application.errors import (
    InvalidPlanningRequest,
    PlanningError,
    PlanningUnavailable,
)
from planning.application.interfaces import PlanningRunRepository
from planning.application.normalizer import PlanningInputNormalizer
from planning.application.validator import PlanningValidator
from planning.infrastructure.adapters.solver import OrToolsPlanningSolver
from planning.infrastructure.adapters.travel_factory import (
    TravelMatrixProviderFactory,
)

logger = logging.getLogger(__name__)


class PlanningService:
    def __init__(
        self,
        identity_provider: IdentityProvider,
        repository: PlanningRunRepository,
        normalizer: PlanningInputNormalizer,
        matrix_factory: TravelMatrixProviderFactory,
        validator: PlanningValidator,
    ):
        self._identity_provider = identity_provider
        self._repository = repository
        self._normalizer = normalizer
        self._matrix_factory = matrix_factory
        self._validator = validator

    async def start_run(
        self, project_id: int, planning_date: date, timezone_name: str
    ) -> dict:
        await self._require_role(UserRoleEnum.ADMIN)
        self._validate_date(planning_date, timezone_name)
        run_id = await self._repository.create_run(
            project_id, planning_date, timezone_name
        )
        logger.info(
            "planning_run_started project_id=%s planning_run_id=%s",
            project_id,
            run_id,
        )
        try:
            source = await self._repository.load_source(project_id, planning_date)
            data = await self._normalizer.normalize(
                project_id, planning_date, timezone_name, source
            )
            await self._repository.mark_running(run_id, data)
            matrix_provider = self._matrix_factory.create(data.config.travel_provider)
            result = await OrToolsPlanningSolver(matrix_provider).solve(data)
            result.validation_errors = self._validator.validate(data, result)
            await self._repository.save_result(run_id, data, result)
            if result.validation_errors:
                raise PlanningUnavailable(
                    "Planning result failed independent validation", run_id=run_id
                )
            logger.info(
                "planning_run_finished project_id=%s planning_run_id=%s "
                "assigned=%s unassigned=%s objective=%s",
                project_id,
                run_id,
                sum(len(route.jobs) for route in result.routes),
                len(result.unassigned),
                result.objective,
            )
            return {
                "planning_run_id": run_id,
                "status": "SUCCESS",
                "solver_status": result.solver_status,
                "metrics": {
                    "objective": result.objective,
                    "drop_cost": result.drop_cost,
                    "travel_cost": result.travel_cost,
                    "assigned_jobs_count": sum(
                        len(route.jobs) for route in result.routes
                    ),
                    "unassigned_jobs_count": len(result.unassigned),
                    "solver_time_ms": result.solver_time_ms,
                },
            }
        except PlanningUnavailable as error:
            if error.run_id is None:
                await self._repository.fail_run(run_id, error.code, error.message)
                error.run_id = run_id
            raise
        except PlanningError as error:
            await self._repository.fail_run(run_id, error.code, error.message)
            error.run_id = run_id
            raise
        except Exception as error:
            logger.exception(
                "planning_run_failed project_id=%s planning_run_id=%s",
                project_id,
                run_id,
            )
            await self._repository.fail_run(
                run_id, "PLANNING_TECHNICAL_ERROR", str(error)
            )
            raise PlanningUnavailable(
                "Planning calculation failed", run_id=run_id
            ) from error

    async def get_run(self, project_id: int, run_id: int) -> dict:
        await self._require_role(UserRoleEnum.USER)
        return await self._repository.get_run(project_id, run_id)

    async def list_runs(
        self,
        project_id: int,
        planning_date: date | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[dict]:
        await self._require_role(UserRoleEnum.USER)
        return await self._repository.list_runs(
            project_id, planning_date, status, min(limit, 100), offset
        )

    async def _require_role(self, required: UserRoleEnum) -> None:
        role = await self._identity_provider.get_role()
        if not has_required_role(role, required):
            raise AccessControlError("The required role does not exist.")

    @staticmethod
    def _validate_date(planning_date: date, timezone_name: str) -> None:
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise InvalidPlanningRequest("Unknown IANA timezone") from error
        current_date = datetime.now(timezone.utc).astimezone(zone).date()
        if planning_date < current_date:
            raise InvalidPlanningRequest(
                "planning_date cannot be in the past for the supplied timezone"
            )
