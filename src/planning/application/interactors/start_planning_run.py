import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from auth.application.interfaces.identity_provider import IdentityProvider
from auth.domain.errors import AccessControlError
from auth.domain.user_role import UserRoleEnum, is_dispatcher
from planning.application.errors import (
    InvalidPlanningRequest,
    PlanningError,
    PlanningUnavailable,
    ProjectNotFound,
)
from planning.application.interfaces.planning_run_repository import (
    PlanningRunRepository,
)
from planning.application.interfaces.planning_solver import PlanningSolverFactory
from planning.application.services.planning_input_normalizer import (
    PlanningInputNormalizer,
)
from planning.application.validators.planning_result import PlanningValidator

logger = logging.getLogger(__name__)


class StartPlanningRunInteractor:
    def __init__(
        self,
        identity_provider: IdentityProvider,
        repository: PlanningRunRepository,
        normalizer: PlanningInputNormalizer,
        solver_factory: PlanningSolverFactory,
        validator: PlanningValidator,
    ):
        self._identity_provider = identity_provider
        self._repository = repository
        self._normalizer = normalizer
        self._solver_factory = solver_factory
        self._validator = validator

    async def __call__(
        self, project_id: int, planning_date: date, timezone_name: str | None
    ) -> dict:
        user = await self._require_project(project_id)
        project_timezone = await self._repository.get_project_timezone(project_id)
        if timezone_name is not None and timezone_name != project_timezone:
            raise InvalidPlanningRequest(
                "timezone must match the project planning timezone"
            )
        timezone_name = project_timezone
        self._validate_date(planning_date, timezone_name)
        run_id = await self._repository.create_run(
            project_id, planning_date, timezone_name, user.id
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
            solver = self._solver_factory.create(data.config.travel_provider)
            result = await solver.solve(data)
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

    async def _require_project(self, project_id: int):
        user = await self._identity_provider.get_current_user()
        if user.role is UserRoleEnum.OWNER:
            return user
        if not is_dispatcher(user.role):
            raise AccessControlError("You do not have access to this project.")
        if user.project_id != project_id:
            raise ProjectNotFound("Project not found")
        await self._repository.get_project_timezone(project_id)
        return user

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
