class PlanningError(Exception):
    code = "PLANNING_ERROR"
    http_status = 500

    def __init__(self, message: str, *, run_id: int | None = None):
        super().__init__(message)
        self.message = message
        self.run_id = run_id


class ProjectNotFound(PlanningError):
    code = "PROJECT_NOT_FOUND"
    http_status = 404


class PlanningRunNotFound(PlanningError):
    code = "PLANNING_RUN_NOT_FOUND"
    http_status = 404


class PlanningRunInProgress(PlanningError):
    code = "PLANNING_RUN_IN_PROGRESS"
    http_status = 409


class InvalidPlanningRequest(PlanningError):
    code = "INVALID_PLANNING_REQUEST"
    http_status = 422


class PlanningUnavailable(PlanningError):
    code = "PLANNING_UNAVAILABLE"
    http_status = 503
