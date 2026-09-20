class PlanningError(Exception):
    code = "PLANNING_ERROR"

    def __init__(
        self,
        message: str,
        *,
        run_id: int | None = None,
        code: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.run_id = run_id
        self.details = details or {}
        if code is not None:
            self.code = code


class ProjectNotFound(PlanningError):
    code = "PROJECT_NOT_FOUND"


class PlanningRunNotFound(PlanningError):
    code = "PLANNING_RUN_NOT_FOUND"


class PlanningRunInProgress(PlanningError):
    code = "PLANNING_RUN_IN_PROGRESS"


class InvalidPlanningRequest(PlanningError):
    code = "INVALID_PLANNING_REQUEST"


class PlanningUnavailable(PlanningError):
    code = "PLANNING_UNAVAILABLE"


class RateLimitExceeded(PlanningError):
    code = "RATE_LIMIT_EXCEEDED"


class AccessDeniedError(PlanningError):
    code = "FORBIDDEN"


class ObjectNotFoundError(PlanningError):
    code = "NOT_FOUND"


class ProjectBlockedError(PlanningError):
    code = "PROJECT_BLOCKED"


class InvalidJobStatusError(PlanningError):
    code = "INVALID_STATUS_TRANSITION"


class ConflictError(PlanningError):
    code = "CONFLICT"
