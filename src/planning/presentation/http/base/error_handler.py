from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette import status

from planning.application.errors import (
    AccessDeniedError,
    ConflictError,
    InvalidJobStatusError,
    InvalidPlanningRequest,
    ObjectNotFoundError,
    PlanningError,
    PlanningRunInProgress,
    PlanningRunNotFound,
    PlanningUnavailable,
    ProjectBlockedError,
    ProjectNotFound,
)

ERROR_STATUS_BY_TYPE = {
    AccessDeniedError: status.HTTP_403_FORBIDDEN,
    ConflictError: status.HTTP_409_CONFLICT,
    InvalidJobStatusError: status.HTTP_409_CONFLICT,
    InvalidPlanningRequest: status.HTTP_422_UNPROCESSABLE_ENTITY,
    ObjectNotFoundError: status.HTTP_404_NOT_FOUND,
    PlanningRunInProgress: status.HTTP_409_CONFLICT,
    PlanningRunNotFound: status.HTTP_404_NOT_FOUND,
    PlanningUnavailable: status.HTTP_503_SERVICE_UNAVAILABLE,
    ProjectBlockedError: status.HTTP_403_FORBIDDEN,
    ProjectNotFound: status.HTTP_404_NOT_FOUND,
}


def planning_error_response(error: PlanningError) -> JSONResponse:
    content: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.run_id is not None:
        content["planning_run_id"] = error.run_id
    return JSONResponse(
        content=content,
        status_code=ERROR_STATUS_BY_TYPE.get(
            type(error),
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        ),
    )


async def handle_planning_error(
    _: Request,
    error: PlanningError,
) -> JSONResponse:
    return planning_error_response(error)


def init_planning_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(PlanningError, handle_planning_error)
